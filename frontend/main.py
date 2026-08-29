"""FastAPI proxy with API Key Authentication, Private GCS Image Persistence & Secure Authenticated Image Serving."""

import asyncio
import io
import json
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
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
    Query,
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
INGEST_JOB_SUFFIX = ".ingest.json"
INGEST_DRAIN_INTERVAL_SEC = float(os.environ.get("INGEST_DRAIN_INTERVAL_SEC", "15"))

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
_RECEIPT_LINE_RE = re.compile(
    r"RECEIPT:\s*(\{.*?\})\s*[^\n]*$", re.MULTILINE | re.DOTALL
)
_RECEIPT_FIELD_KEYS = ("merchant", "amount", "currency", "date")


def extract_receipt_fields(text: str | None) -> dict[str, str | None]:
    """Parse merchant/amount/currency/date from an agent receipt reply."""
    empty = dict.fromkeys(_RECEIPT_FIELD_KEYS)
    if not text:
        return empty
    payload = None
    match = _RECEIPT_LINE_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        for candidate in re.finditer(r"\{[^{}]+\}", text):
            try:
                obj = json.loads(candidate.group(0))
            except json.JSONDecodeError:
                continue
            if any(key in obj for key in _RECEIPT_FIELD_KEYS):
                payload = obj
                break
    if not isinstance(payload, dict):
        return empty
    out = dict(empty)
    for key in _RECEIPT_FIELD_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    return out


def strip_receipt_marker(text: str | None) -> str:
    """Remove the machine-readable RECEIPT: line from user-facing prose."""
    if not text:
        return ""
    return _RECEIPT_LINE_RE.sub("", text).strip()


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


_ingest_tasks: set[asyncio.Task] = set()
_ingest_in_flight: set[str] = set()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    worker = asyncio.create_task(_run_ingest_drain_worker())
    _ingest_tasks.add(worker)
    try:
        yield
    finally:
        worker.cancel()
        _ingest_tasks.discard(worker)
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title="Visual Memory Vault Proxy", lifespan=_lifespan)


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
    kick_ingest_drain()
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


def persist_uploaded_image(file_bytes: bytes, image_name: str, media_type: str) -> str:
    """Save the image to MEDIA_DIR and optional GCS. Returns the protected /media path."""
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

    return protected_url


def ingest_job_name(image_name: str) -> str:
    return f"{image_name}{INGEST_JOB_SUFFIX}"


def persist_ingest_job(
    image_name: str,
    filename: str,
    media_type: str,
    protected_url: str,
    subject: str | None,
) -> dict:
    """Write a durable ingest job next to the image (MEDIA_DIR and optional GCS)."""
    job = {
        "image_name": image_name,
        "filename": filename,
        "media_type": media_type,
        "protected_url": protected_url,
        "subject": subject,
    }
    payload = json.dumps(job)
    job_name = ingest_job_name(image_name)

    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with open(os.path.join(MEDIA_DIR, job_name), "w") as f:
            f.write(payload)
    except Exception as e:
        print(f"Warning: Failed to persist ingest job locally: {e}")

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(f"vault-images/{job_name}")
                blob.upload_from_string(payload, content_type="application/json")
        except Exception as e:
            print(f"Warning: Failed to persist ingest job to GCS: {e}")

    return job


def complete_ingest_job(image_name: str) -> None:
    """Remove a finished ingest job from MEDIA_DIR and optional GCS."""
    job_name = ingest_job_name(image_name)
    local_path = os.path.join(MEDIA_DIR, job_name)
    try:
        if os.path.exists(local_path):
            os.remove(local_path)
    except Exception as e:
        print(f"Warning: Failed to remove local ingest job: {e}")

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(f"vault-images/{job_name}")
                if blob.exists():
                    blob.delete()
        except Exception as e:
            print(f"Warning: Failed to remove GCS ingest job: {e}")


def load_uploaded_image_bytes(image_name: str) -> bytes | None:
    local_path = os.path.join(MEDIA_DIR, image_name)
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            return f.read()

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(f"vault-images/{image_name}")
                if blob.exists():
                    return blob.download_as_bytes()
        except Exception as e:
            print(f"Warning: Failed to load image for ingest job: {e}")
    return None


def _read_ingest_job_file(path: str) -> dict | None:
    try:
        with open(path) as f:
            job = json.load(f)
    except Exception as e:
        print(f"Warning: Failed to read ingest job {path}: {e}")
        return None
    if isinstance(job, dict) and job.get("image_name"):
        return job
    return None


def list_pending_ingest_jobs() -> list[dict]:
    """List durable ingest jobs from MEDIA_DIR and optional GCS."""
    seen: dict[str, dict] = {}
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        for name in os.listdir(MEDIA_DIR):
            if not name.endswith(INGEST_JOB_SUFFIX):
                continue
            job = _read_ingest_job_file(os.path.join(MEDIA_DIR, name))
            if job:
                seen[job["image_name"]] = job
    except Exception as e:
        print(f"Warning: Failed to list local ingest jobs: {e}")

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                bucket = gcs.bucket(GCS_BUCKET_NAME)
                for blob in bucket.list_blobs(prefix="vault-images/"):
                    if not blob.name.endswith(INGEST_JOB_SUFFIX):
                        continue
                    try:
                        job = json.loads(blob.download_as_text())
                    except Exception as e:
                        print(
                            f"Warning: Failed to read GCS ingest job {blob.name}: {e}"
                        )
                        continue
                    if isinstance(job, dict) and job.get("image_name"):
                        seen.setdefault(job["image_name"], job)
        except Exception as e:
            print(f"Warning: Failed to list GCS ingest jobs: {e}")

    return list(seen.values())


async def process_ingest_job(job: dict, file_bytes: bytes | None = None) -> bool:
    """Run extract+store for one persisted job. Leaves the job on failure."""
    image_name = job["image_name"]
    if image_name in _ingest_in_flight:
        return False
    _ingest_in_flight.add(image_name)
    try:
        data = (
            file_bytes
            if file_bytes is not None
            else load_uploaded_image_bytes(image_name)
        )
        if not data:
            print(f"Warning: ingest job image missing for {image_name}")
            return False
        await ingest_uploaded_image(
            data,
            job["filename"],
            job["media_type"],
            job["protected_url"],
            job.get("subject"),
        )
        complete_ingest_job(image_name)
        return True
    finally:
        _ingest_in_flight.discard(image_name)


async def drain_pending_ingest_jobs(limit: int | None = None) -> list[str]:
    """Process persisted ingest jobs left after a 202 / scale-to-zero."""
    completed: list[str] = []
    jobs = list_pending_ingest_jobs()
    if limit is not None:
        jobs = jobs[:limit]
    for job in jobs:
        try:
            if await process_ingest_job(job):
                completed.append(job["image_name"])
        except Exception as exc:
            print(f"Error: drain ingest failed for {job.get('protected_url')}: {exc}")
    return completed


async def _run_ingest_drain_worker() -> None:
    interval = INGEST_DRAIN_INTERVAL_SEC
    if interval <= 0:
        return
    while True:
        try:
            await drain_pending_ingest_jobs()
        except Exception as exc:
            print(f"Error: ingest drain worker failed: {exc}")
        await asyncio.sleep(interval)


def kick_ingest_drain() -> None:
    """Best-effort drain on a later request if the first instance died."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(drain_pending_ingest_jobs())
    _ingest_tasks.add(task)
    task.add_done_callback(_ingest_tasks.discard)


def _upload_ingest_prompt(
    filename: str, protected_url: str, subject: str | None
) -> str:
    return (
        f"I uploaded a photo/screenshot named '{filename}'. "
        f"Protected Media Relative Path: {protected_url or 'N/A'}. "
        f"Subject context: {subject or 'Mobile upload'}. "
        "Extract key text, details, and context from this image and store it into my Flair visual memory. "
        f"Pass image_url='{protected_url}' when storing the memory. "
        "If this image is a receipt or invoice, extract merchant, amount, currency, and date, "
        "pass them in store_memory custom_metadata together with image_url, keep the prose description, "
        'and include one reply line of the form RECEIPT: {"merchant":"...","amount":"...","currency":"...","date":"..."}'
    )


async def ingest_uploaded_image(
    file_bytes: bytes,
    filename: str,
    media_type: str,
    protected_url: str,
    subject: str | None,
) -> str:
    """Send the image through A2A for extract + store_memory. Returns reply text."""
    prompt = _upload_ingest_prompt(filename, protected_url, subject)

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

    return (
        "\n".join([p["text"] for p in parts if p.get("kind") == "text"])
        or "Photo processed and saved to visual memory."
    )


async def _background_ingest(
    file_bytes: bytes,
    filename: str,
    media_type: str,
    protected_url: str,
    subject: str | None,
    image_name: str,
) -> None:
    job = {
        "image_name": image_name,
        "filename": filename,
        "media_type": media_type,
        "protected_url": protected_url,
        "subject": subject,
    }
    try:
        await process_ingest_job(job, file_bytes=file_bytes)
    except Exception as exc:
        print(f"Error: Background memory ingest failed for {protected_url}: {exc}")


def schedule_memory_ingest(
    file_bytes: bytes,
    filename: str,
    media_type: str,
    protected_url: str,
    subject: str | None,
    image_name: str,
) -> None:
    """Start Flair ingest without blocking the HTTP response."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        import threading

        threading.Thread(
            target=lambda: asyncio.run(
                _background_ingest(
                    file_bytes,
                    filename,
                    media_type,
                    protected_url,
                    subject,
                    image_name,
                )
            ),
            daemon=True,
            name="memory-ingest",
        ).start()
        return

    task = loop.create_task(
        _background_ingest(
            file_bytes, filename, media_type, protected_url, subject, image_name
        )
    )
    _ingest_tasks.add(task)
    task.add_done_callback(_ingest_tasks.discard)


def _receipt_response_fields(reply_text: str) -> dict[str, str | None]:
    receipt = extract_receipt_fields(reply_text)
    summary = strip_receipt_marker(reply_text) or reply_text
    return {
        "summary": summary,
        "reply": summary,
        "merchant": receipt["merchant"],
        "amount": receipt["amount"],
        "currency": receipt["currency"],
        "date": receipt["date"],
    }


@app.post("/upload")
async def upload_image(
    req: Request,
    file: Annotated[UploadFile, File(...)],
    subject: Annotated[str | None, Form()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    wait: Annotated[bool, Query()] = False,
):
    """Accept a photo from iOS Shortcuts (202 send-and-forget) or the web UI (?wait=1)."""
    verify_api_key(req, x_api_key)
    raw_bytes = await file.read()
    raw_filename = file.filename or "uploaded_photo.jpg"

    # Normalize HEIC / iOS images to clean JPEG
    file_bytes, filename, media_type = normalize_image(
        raw_bytes, raw_filename, file.content_type
    )

    image_name = f"{uuid.uuid4()}_{filename}"
    protected_url = persist_uploaded_image(file_bytes, image_name, media_type)

    if wait:
        kick_ingest_drain()
        reply_text = await ingest_uploaded_image(
            file_bytes, filename, media_type, protected_url, subject
        )
        return JSONResponse(
            {
                "status": "success",
                "filename": filename,
                "image_path": protected_url,
                **_receipt_response_fields(reply_text),
            }
        )

    persist_ingest_job(image_name, filename, media_type, protected_url, subject)
    schedule_memory_ingest(
        file_bytes, filename, media_type, protected_url, subject, image_name
    )
    return JSONResponse(
        {
            "status": "accepted",
            "filename": filename,
            "image_path": protected_url,
        },
        status_code=202,
    )


@app.api_route("/media/{image_name}", methods=["GET", "HEAD"])
async def get_media(
    image_name: str, req: Request, x_api_key: str | None = Header(None)
):
    """Authenticated image retrieval endpoint requiring API Key verification."""
    verify_api_key(req, x_api_key)
    kick_ingest_drain()

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


@app.get("/health")
async def health():
    """Liveness probe; also drains persisted ingest jobs if a prior 202 died."""
    kick_ingest_drain()
    return JSONResponse({"status": "ok"})


@app.post("/ingest")
async def ingest_pending(
    req: Request, x_api_key: Annotated[str | None, Header()] = None
):
    """Internal: run one persisted ingest job. Shortcut clients must not call this."""
    verify_api_key(req, x_api_key)
    completed = await drain_pending_ingest_jobs(limit=1)
    return JSONResponse({"status": "ok", "completed": completed})


static_dir = "static" if os.path.exists("static") else "frontend/static"
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
