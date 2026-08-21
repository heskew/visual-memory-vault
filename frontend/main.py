"""FastAPI proxy with API Key Authentication, Private GCS Image Persistence & Secure Authenticated Image Serving."""

import base64
import os
import uuid

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
from fastapi import FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import storage
from google.protobuf.json_format import ParseDict

RESOURCE = os.environ.get(
    "AGENT_ENGINE_RESOURCE_NAME",
    "projects/621065712696/locations/us-east1/reasoningEngines/1086440633544998912",
)
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")
LOCATION = (
    RESOURCE.split("/locations/")[1].split("/")[0] if "/locations/" in RESOURCE else "us-east1"
)
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "bwg3-qwiklabs-gcp-04-4fe84a121fc3")

A2A_BASE = (
    f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
)
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
            meta = getattr(p, "metadata", None) if not isinstance(p, dict) else p.get("metadata")
            meta = meta or {}
            mime = meta.get("mimeType") if isinstance(meta, dict) else getattr(meta, "mime_type", None)
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


@app.post("/upload")
async def upload_image(
    req: Request,
    file: UploadFile = File(...),
    subject: str | None = None,
    x_api_key: str | None = Header(None),
):
    """Endpoint for iOS Shortcuts and mobile apps to upload photos/screenshots."""
    verify_api_key(req, x_api_key)
    file_bytes = await file.read()
    filename = file.filename or "uploaded_photo.jpg"

    # 1. Save original photo privately to GCS
    image_name = f"{uuid.uuid4()}_{filename}"
    blob_id = f"vault-images/{image_name}"
    protected_url = None

    try:
        gcs = _get_gcs_client()
        bucket = gcs.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(blob_id)
        blob.upload_from_string(
            file_bytes, content_type=file.content_type or "image/jpeg"
        )
        protected_url = f"/media/{image_name}"
    except Exception as e:
        print(f"Warning: Failed to upload image to GCS: {e}")

    # 2. Build prompt for agent with authenticated proxy URL & image bytes
    prompt = (
        f"I uploaded a photo/screenshot named '{filename}'. "
        f"Protected Media Relative Path: {protected_url or 'N/A'}. "
        f"Subject context: {subject or 'Mobile upload'}. "
        "Extract key text, details, and context from this image and store it into my FLAIR visual memory. "
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
                    media_type=file.content_type or "image/jpeg",
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


@app.get("/media/{image_name}")
async def get_media(
    image_name: str, req: Request, x_api_key: str | None = Header(None)
):
    """Authenticated image retrieval endpoint requiring API Key verification."""
    verify_api_key(req, x_api_key)
    try:
        gcs = _get_gcs_client()
        bucket = gcs.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(f"vault-images/{image_name}")
        if not blob.exists():
            raise HTTPException(status_code=404, detail="Image not found")
        content = blob.download_as_bytes()
        return Response(content=content, media_type=blob.content_type or "image/jpeg")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve image: {e}")


static_dir = "static" if os.path.exists("static") else "frontend/static"
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
