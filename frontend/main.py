"""FastAPI proxy with API Key Authentication, Private GCS Image Persistence & Secure Authenticated Image Serving."""

import io
import os
import uuid
from typing import Annotated

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
)
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import storage
from google.protobuf.json_format import ParseDict
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
MEDIA_DIR = os.path.abspath(os.environ.get("MEDIA_DIR", "./media"))

if "A2A_BASE_URL" in os.environ:
    A2A_BASE = os.environ["A2A_BASE_URL"]
elif RESOURCE:
    LOCATION = (
        RESOURCE.split("/locations/")[1].split("/")[0]
        if "/locations/" in RESOURCE
        else "us-east1"
    )
    A2A_BASE = (
        f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
        f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
    )
else:
    A2A_BASE = f"http://127.0.0.1:{os.environ.get('BACKEND_PORT', '8000')}/a2a/{AGENT_DIRECTORY}"

A2A_CARD_URL = f"{A2A_BASE}/.well-known/agent-card.json"

_A2UI_MIME = "application/json+a2ui"
API_KEY = os.environ.get("PROXY_API_KEY", "")

_creds = None


def _get_creds():
    global _creds
    if _creds is None:
        try:
            _creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except Exception as e:
            print(f"Warning: google.auth.default failed: {e}")
            return None
    return _creds


def _auth_headers() -> dict[str, str]:
    creds = _get_creds()
    headers = {"Content-Type": "application/json"}
    if creds:
        try:
            creds.refresh(google.auth.transport.requests.Request())
            headers["Authorization"] = f"Bearer {creds.token}"
        except Exception as e:
            print(f"Warning: Failed to refresh Google credentials: {e}")
    return headers


app = FastAPI(title="Visual Memory Vault Proxy")


def verify_api_key(req: Request, x_api_key: str | None = None):
    key = x_api_key or req.headers.get("X-Api-Key") or req.query_params.get("key")
    if not API_KEY or key == API_KEY:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized: Invalid API Key")


_contexts: dict[str, str] = {}
_card: AgentCard | None = None
_gcs_client: storage.Client | None = None


def _get_gcs_client() -> storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = storage.Client()
    return _gcs_client


async def _get_card(client: httpx.AsyncClient) -> AgentCard:
    global _card
    if _card is None:
        resp = await client.get(A2A_CARD_URL)
        resp.raise_for_status()
        card = AgentCard()
        ParseDict(resp.json(), card, ignore_unknown_fields=True)
        if card.supported_interfaces:
            card.supported_interfaces[0].url = A2A_BASE
        else:
            card.supported_interfaces.add(url=A2A_BASE)
        _card = card
    return _card


def _extract_parts(parts: list) -> list[dict]:
    out: list[dict] = []
    for p in parts:
        txt = getattr(p, "text", None) if not isinstance(p, dict) else p.get("text")
        if txt:
            out.append({"kind": "text", "text": txt})
            continue

        data = getattr(p, "data", None) if not isinstance(p, dict) else p.get("data")
        if data is not None:
            meta = (
                getattr(p, "metadata", None)
                if not isinstance(p, dict)
                else p.get("metadata")
            )
            meta = meta or {}
            mime = (
                meta.get("mimeType")
                if isinstance(meta, dict)
                else getattr(meta, "mime_type", None)
            )
            if mime == _A2UI_MIME:
                out.append({"kind": "a2ui", "data": data})
    return out


@app.post("/chat")
async def chat(req: Request):
    verify_api_key(req)
    body = await req.json()
    message = body.get("message", "")
    user_id = body.get("user_id") or "web-user"
    parts: list[dict] = []

    async with httpx.AsyncClient(headers=_auth_headers(), timeout=120) as client:
        card = await _get_card(client)
        factory = ClientFactory(ClientConfig(httpx_client=client))
        a2a_client = factory.create(card)

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=message)],
            context_id=_contexts.get(user_id),
        )
        send_req = SendMessageRequest(
            message=msg, configuration=SendMessageConfiguration()
        )

        async for event in a2a_client.send_message(send_req):
            if hasattr(event, "HasField"):
                if event.HasField("task"):
                    if event.task.context_id:
                        _contexts[user_id] = event.task.context_id
                if event.HasField("artifact_update"):
                    parts.extend(_extract_parts(event.artifact_update.artifact.parts))

    if not parts:
        parts = [{"kind": "text", "text": "(The agent didn't return a reply.)"}]
    return JSONResponse({"parts": parts})


def normalize_image(
    file_bytes: bytes, filename: str, content_type: str | None
) -> tuple[bytes, str, str]:
    """Convert HEIC/HEIF and raw image formats to standard RGB JPEG for Gemini."""
    try:
        image = Image.open(io.BytesIO(file_bytes))
        if image.mode != "RGB":
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=90)
        jpeg_bytes = out.getvalue()
        base_name = os.path.splitext(filename)[0]
        return jpeg_bytes, f"{base_name}.jpg", "image/jpeg"
    except Exception as exc:
        print(f"Warning: Image normalization note: {exc}")
        return file_bytes, filename, content_type or "image/jpeg"


@app.post("/upload")
async def upload_image(
    req: Request,
    file: Annotated[UploadFile, File(...)],
    subject: Annotated[str | None, Form()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
):
    """Endpoint for iOS Shortcuts and mobile apps to upload photos/screenshots."""
    verify_api_key(req, x_api_key)
    raw_bytes = await file.read()
    raw_filename = file.filename or "uploaded_photo.jpg"

    # Normalize HEIC / iOS images to clean JPEG
    file_bytes, filename, media_type = normalize_image(
        raw_bytes, raw_filename, file.content_type
    )

    # 1. Save original photo (local disk + optional GCS)
    image_name = f"{uuid.uuid4()}_{filename}"
    blob_id = f"vault-images/{image_name}"
    protected_url = f"/media/{image_name}"

    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        local_path = os.path.join(MEDIA_DIR, image_name)
        with open(local_path, "wb") as f:
            f.write(file_bytes)
    except Exception as e:
        print(f"Warning: Failed to save image locally: {e}")

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                bucket = gcs.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(blob_id)
                blob.upload_from_string(file_bytes, content_type=media_type)
        except Exception as e:
            print(f"Warning: Failed to upload image to GCS: {e}")

    # 2. Build prompt for agent with authenticated proxy URL & image bytes
    prompt = (
        f"I uploaded a photo/screenshot named '{filename}'. "
        f"Protected Media Relative Path: {protected_url or 'N/A'}. "
        f"Subject context: {subject or 'Mobile upload'}. "
        "Extract key text, details, and context from this image and store it into my Flair visual memory. "
        f"Pass image_url='{protected_url}' when storing the memory."
    )

    async with httpx.AsyncClient(headers=_auth_headers(), timeout=120) as client:
        card = await _get_card(client)
        factory = ClientFactory(ClientConfig(httpx_client=client))
        a2a_client = factory.create(card)

        msg = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[
                Part(text=prompt),
                Part(
                    raw=file_bytes,
                    media_type=media_type,
                    filename=filename,
                ),
            ],
            context_id=_contexts.get("mobile-user"),
        )
        send_req = SendMessageRequest(
            message=msg, configuration=SendMessageConfiguration()
        )

        parts: list[dict] = []
        async for event in a2a_client.send_message(send_req):
            if hasattr(event, "HasField"):
                if event.HasField("task"):
                    if event.task.context_id:
                        _contexts["mobile-user"] = event.task.context_id
                if event.HasField("artifact_update"):
                    parts.extend(_extract_parts(event.artifact_update.artifact.parts))

    reply_text = (
        "\n".join([p["text"] for p in parts if p.get("kind") == "text"])
        or "Photo processed and saved to visual memory."
    )

    return JSONResponse(
        {
            "status": "success",
            "filename": filename,
            "image_path": protected_url,
            "summary": reply_text,
            "reply": reply_text,
        }
    )


@app.api_route("/media/{image_name}", methods=["GET", "HEAD"])
async def get_media(
    image_name: str, req: Request, x_api_key: str | None = Header(None)
):
    """Authenticated image retrieval endpoint requiring API Key verification."""
    verify_api_key(req, x_api_key)

    # 1. Check local media directory first
    local_path = os.path.join(MEDIA_DIR, image_name)
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            content = f.read()
        return Response(content=content, media_type="image/jpeg")

    # 2. Check GCS bucket if configured
    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                bucket = gcs.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(f"vault-images/{image_name}")
                if blob.exists():
                    content = blob.download_as_bytes()
                    return Response(
                        content=content, media_type=blob.content_type or "image/jpeg"
                    )
        except Exception as e:
            print(f"Warning: GCS image fetch failed: {e}")

    raise HTTPException(status_code=404, detail="Image not found")


static_dir = "static" if os.path.exists("static") else "frontend/static"
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
