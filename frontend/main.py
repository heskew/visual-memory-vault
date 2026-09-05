"""FastAPI proxy with API Key Authentication, Private GCS Image Persistence & Secure Authenticated Image Serving."""

import asyncio
import io
import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Annotated

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import (
    A2AClientError,
    A2AClientTimeoutError,
    AgentCardResolutionError,
    ClientConfig,
    ClientFactory,
)
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
JOBS_DIR_NAME = "jobs"
JOBS_GCS_PREFIX = "vault-jobs/"
# Production worker is Cloud Tasks → POST /ingest. The lifespan loop is local
# uvicorn only; default 0 so Cloud Run does not pretend CPU-after-response is a worker.
INGEST_DRAIN_INTERVAL_SEC = float(os.environ.get("INGEST_DRAIN_INTERVAL_SEC", "0"))
CLOUD_TASKS_QUEUE = os.environ.get("CLOUD_TASKS_QUEUE")
CLOUD_TASKS_LOCATION = os.environ.get("CLOUD_TASKS_LOCATION") or os.environ.get(
    "GOOGLE_CLOUD_LOCATION"
)
CLOUD_TASKS_PROJECT = os.environ.get("CLOUD_TASKS_PROJECT") or os.environ.get(
    "GOOGLE_CLOUD_PROJECT"
)
# Operator-provided Cloud Run / proxy base. Never invent a hosted URL.
INGEST_HANDLER_URL = os.environ.get("INGEST_HANDLER_URL")
INGEST_TASKS_OIDC_SA = os.environ.get("INGEST_TASKS_OIDC_SA")
# Lease must outlive the longest possible ingest, i.e. the 120s A2A client
# timeout in ingest_uploaded_image, so a still-running job is not reclaimed by
# a second worker and processed (and stored) twice. Keep comfortably above 120.
INGEST_LEASE_SEC = float(os.environ.get("INGEST_LEASE_SEC", "300"))

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
_job_write_lock = threading.Lock()
_tasks_client = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """No production worker here. Cloud Tasks allocates CPU for POST /ingest."""
    if CLOUD_TASKS_QUEUE or INGEST_DRAIN_INTERVAL_SEC <= 0:
        yield
        return
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


class PersistError(Exception):
    """Durable image persist failed."""


class EnqueueError(Exception):
    """Durable job enqueue failed."""


class TransientIngestError(Exception):
    """Retryable extract/store failure. Job stays pending."""


class TerminalIngestError(Exception):
    """Permanent extract/store failure. Job is marked failed."""


_A2A_HTTP_ERROR_RE = re.compile(r"HTTP Error (\d+):")


def _http_status_from_ingest_exc(exc: BaseException) -> int | None:
    """Status from httpx, AgentCardResolutionError, or A2AClientError('HTTP Error N:')."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, AgentCardResolutionError) and exc.status_code is not None:
        return exc.status_code
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code
    if isinstance(exc, A2AClientError):
        match = _A2A_HTTP_ERROR_RE.match(str(exc))
        if match:
            return int(match.group(1))
    return None


def is_transient_ingest_error(exc: BaseException) -> bool:
    """A2A/Flair timeouts and 5xx/429 stay pending; 4xx and programmer errors fail."""
    if isinstance(exc, TerminalIngestError):
        return False
    if isinstance(
        exc, (TypeError, KeyError, AttributeError, AssertionError, NameError)
    ):
        return False
    if isinstance(exc, (TransientIngestError, A2AClientTimeoutError)):
        return True
    code = _http_status_from_ingest_exc(exc)
    if code is not None:
        return code == 429 or code >= 500
    if isinstance(
        exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError)
    ):
        return True
    if isinstance(exc, A2AClientError):
        return True
    return False


def persist_uploaded_image(file_bytes: bytes, image_name: str, media_type: str) -> str:
    """Save the image to MEDIA_DIR and optional GCS. Raises if the durable copy fails."""
    blob_id = f"vault-images/{image_name}"
    protected_url = f"/media/{image_name}"
    local_ok = False
    gcs_ok = False

    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        local_path = os.path.join(MEDIA_DIR, image_name)
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        local_ok = True
    except Exception as e:
        print(f"Warning: Failed to save image locally: {e}")

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                bucket = gcs.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(blob_id)
                blob.upload_from_string(file_bytes, content_type=media_type)
                gcs_ok = True
        except Exception as e:
            print(f"Warning: Failed to upload image to GCS: {e}")
        if not gcs_ok:
            raise PersistError("durable image persist failed")
        return protected_url

    if not local_ok:
        raise PersistError("local image persist failed")
    return protected_url


def parse_job_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


def _job_local_path(job_id: str) -> str:
    return os.path.join(MEDIA_DIR, JOBS_DIR_NAME, f"{job_id}.json")


def _store_commit_path(job_id: str) -> str:
    return os.path.join(MEDIA_DIR, JOBS_DIR_NAME, f"{job_id}.stored.json")


def _job_gcs_name(job_id: str) -> str:
    return f"{JOBS_GCS_PREFIX}{job_id}.json"


def _store_commit_gcs_name(job_id: str) -> str:
    return f"{JOBS_GCS_PREFIX}{job_id}.stored.json"


def _is_job_json_name(name: str) -> bool:
    return name.endswith(".json") and not name.endswith(".stored.json")


def _is_drainable(job: dict | None) -> bool:
    if not job:
        return False
    status = job.get("status")
    if status == "pending":
        return True
    return status == "running" and _lease_expired(job)


def _write_is_stale(current: dict, incoming: dict) -> bool:
    """A stale worker must not replace a newer lease or a terminal status."""
    cur_status = current.get("status")
    new_status = incoming.get("status")
    if cur_status == "succeeded" and new_status != "succeeded":
        return True
    if cur_status == "failed" and new_status not in {"failed", "succeeded"}:
        return True
    cur_lease = current.get("lease_id")
    new_lease = incoming.get("lease_id")
    if (
        cur_status == "running"
        and cur_lease
        and cur_lease != new_lease
        and not _lease_expired(current)
    ):
        return True
    return False


def _decode_job_payload(raw: str | bytes) -> dict | None:
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Warning: Failed to decode job record: {e}")
        return None
    if isinstance(job, dict) and job.get("job_id"):
        return job
    return None


def write_job_record(job: dict) -> None:
    """Persist a job record with generation match. GCS is required when configured."""
    from google.api_core import exceptions as gcs_exceptions

    payload = json.dumps(job)
    job_id = job["job_id"]
    local_ok = False
    gcs_ok = False

    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(_job_gcs_name(job_id))
                generation = 0
                if blob.exists():
                    blob.reload()
                    current = _decode_job_payload(blob.download_as_text())
                    if current and _write_is_stale(current, job):
                        raise EnqueueError("stale job write")
                    generation = blob.generation
                blob.upload_from_string(
                    payload,
                    content_type="application/json",
                    if_generation_match=generation,
                )
                gcs_ok = True
        except EnqueueError:
            raise
        except gcs_exceptions.PreconditionFailed as exc:
            raise EnqueueError("stale job write") from exc
        except Exception as e:
            print(f"Warning: Failed to write GCS job record: {e}")
        if not gcs_ok:
            raise EnqueueError("durable job enqueue failed")
        _write_local_job_cache(job)
        return

    try:
        os.makedirs(os.path.join(MEDIA_DIR, JOBS_DIR_NAME), exist_ok=True)
        with _job_write_lock:
            current = _load_local_job(job_id)
            if current and _write_is_stale(current, job):
                raise EnqueueError("stale job write")
            with open(_job_local_path(job_id), "w") as f:
                f.write(payload)
        local_ok = True
    except EnqueueError:
        raise
    except Exception as e:
        print(f"Warning: Failed to write local job record: {e}")

    if not local_ok:
        raise EnqueueError("local job enqueue failed")


def _write_local_job_cache(job: dict) -> None:
    """Best-effort local cache. Never overrides GCS as source of truth."""
    try:
        os.makedirs(os.path.join(MEDIA_DIR, JOBS_DIR_NAME), exist_ok=True)
        with open(_job_local_path(job["job_id"]), "w") as f:
            f.write(json.dumps(job))
    except Exception as e:
        print(f"Warning: Failed to refresh local job cache: {e}")


def _load_local_job(job_id: str) -> dict | None:
    local_path = _job_local_path(job_id)
    if os.path.exists(local_path):
        try:
            with open(local_path) as f:
                return _decode_job_payload(f.read())
        except Exception as e:
            print(f"Warning: Failed to read local job record: {e}")
    return None


def _load_job_record(job_id: str) -> dict | None:
    """Raw job JSON. GCS is source of truth when readable; local cache on GCS errors."""
    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(_job_gcs_name(job_id))
                if blob.exists():
                    job = _decode_job_payload(blob.download_as_text())
                    if job:
                        _write_local_job_cache(job)
                    return job
                return None
        except Exception as e:
            print(f"Warning: Failed to read GCS job record: {e}")
            return _load_local_job(job_id)
        return None

    return _load_local_job(job_id)


def load_job(job_id: str) -> dict | None:
    """Load a job. A durable store-commit wins over a still-pending/running record."""
    record = _load_job_record(job_id)
    mark = _load_store_commit(job_id)
    if mark and mark.get("store_committed"):
        if not record or record.get("status") != "succeeded":
            return mark
    return record


def _durable_job_terminal(job_id: str) -> bool:
    record = _load_job_record(job_id)
    return bool(record and record.get("status") in {"succeeded", "failed"})


def enqueue_ingest_job(
    image_name: str,
    filename: str,
    media_type: str,
    image_path: str,
    subject: str | None,
) -> dict:
    """Write a pending job to the durable store. Raises EnqueueError on failure."""
    job = {
        "job_id": str(uuid.uuid4()),
        "status": "pending",
        "image_name": image_name,
        "filename": filename,
        "media_type": media_type,
        "image_path": image_path,
        "subject": subject,
        "summary": None,
        "reply": None,
        "merchant": None,
        "amount": None,
        "currency": None,
        "date": None,
        "error": None,
    }
    write_job_record(job)
    schedule_ingest_consumer(job["job_id"])
    return job


def _get_tasks_client():
    global _tasks_client
    if _tasks_client is None:
        from google.cloud import tasks_v2

        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def create_ingest_cloud_task(job_id: str) -> None:
    """Enqueue POST /ingest as a new HTTP request. Does not invent a handler URL."""
    if not INGEST_HANDLER_URL:
        raise EnqueueError("INGEST_HANDLER_URL is required to enqueue ingest")
    if not CLOUD_TASKS_PROJECT or not CLOUD_TASKS_LOCATION:
        raise EnqueueError("CLOUD_TASKS_PROJECT and CLOUD_TASKS_LOCATION are required")
    try:
        from google.cloud import tasks_v2

        client = _get_tasks_client()
        parent = client.queue_path(
            CLOUD_TASKS_PROJECT, CLOUD_TASKS_LOCATION, CLOUD_TASKS_QUEUE
        )
        url = INGEST_HANDLER_URL.rstrip("/") + "/ingest"
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["X-Api-Key"] = API_KEY
        http_request = {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": headers,
            "body": json.dumps({"job_id": job_id}).encode(),
        }
        if INGEST_TASKS_OIDC_SA:
            http_request["oidc_token"] = {
                "service_account_email": INGEST_TASKS_OIDC_SA,
                "audience": INGEST_HANDLER_URL.rstrip("/"),
            }
        client.create_task(
            request={"parent": parent, "task": {"http_request": http_request}}
        )
    except EnqueueError:
        raise
    except Exception as exc:
        raise EnqueueError("cloud tasks enqueue failed") from exc


def schedule_ingest_consumer(job_id: str) -> None:
    """Allocate CPU for ingest via Cloud Tasks. Local/dev has no queue."""
    if CLOUD_TASKS_QUEUE:
        create_ingest_cloud_task(job_id)
        return
    if GCS_BUCKET_NAME:
        raise EnqueueError("CLOUD_TASKS_QUEUE is required when GCS_BUCKET_NAME is set")


def _write_local_store_commit(job: dict) -> None:
    try:
        os.makedirs(os.path.join(MEDIA_DIR, JOBS_DIR_NAME), exist_ok=True)
        with open(_store_commit_path(job["job_id"]), "w") as f:
            f.write(json.dumps(job))
    except Exception as e:
        print(f"Warning: Failed to write local store-commit mark: {e}")


def _write_store_commit(job: dict) -> None:
    """Durable already-stored mark (GCS when configured) with generation match."""
    from google.api_core import exceptions as gcs_exceptions

    payload = json.dumps(job)
    job_id = job["job_id"]
    _write_local_store_commit(job)
    if not GCS_BUCKET_NAME:
        if not os.path.exists(_store_commit_path(job_id)):
            raise EnqueueError("local store-commit failed")
        return

    try:
        gcs = _get_gcs_client()
        if not gcs:
            raise EnqueueError("durable store-commit failed")
        blob = gcs.bucket(GCS_BUCKET_NAME).blob(_store_commit_gcs_name(job_id))
        generation = 0
        if blob.exists():
            blob.reload()
            generation = blob.generation
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=generation,
        )
    except EnqueueError:
        raise
    except gcs_exceptions.PreconditionFailed as exc:
        raise EnqueueError("stale store-commit write") from exc
    except Exception as exc:
        raise EnqueueError("durable store-commit failed") from exc


def _load_store_commit(job_id: str) -> dict | None:
    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                blob = gcs.bucket(GCS_BUCKET_NAME).blob(_store_commit_gcs_name(job_id))
                if blob.exists():
                    mark = _decode_job_payload(blob.download_as_text())
                    if mark:
                        _write_local_store_commit(mark)
                    return mark
                return None
        except Exception as e:
            print(f"Warning: Failed to read GCS store-commit: {e}")
            return _load_local_store_commit(job_id)
        return None
    return _load_local_store_commit(job_id)


def _load_local_store_commit(job_id: str) -> dict | None:
    path = _store_commit_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return _decode_job_payload(f.read())
    except Exception as e:
        print(f"Warning: Failed to read store-commit mark: {e}")
        return None


def _lease_expired(job: dict) -> bool:
    until = job.get("lease_until")
    if until is None:
        return True
    try:
        return time.time() >= float(until)
    except (TypeError, ValueError):
        return True


def _prepare_running_lease(job: dict) -> dict:
    claimed = dict(job)
    claimed["status"] = "running"
    claimed["lease_id"] = str(uuid.uuid4())
    claimed["lease_until"] = time.time() + INGEST_LEASE_SEC
    return claimed


def _claim_local_job(job_id: str) -> dict | None:
    current = _load_local_job(job_id)
    if not current:
        return None
    status = current.get("status")
    if status in {"succeeded", "failed"}:
        return None
    if status == "running" and not _lease_expired(current):
        return None
    if status not in {"pending", "running"}:
        return None
    claimed = _prepare_running_lease(current)
    _write_local_job_cache(claimed)
    return claimed


def _claim_gcs_job(job_id: str) -> dict | None:
    from google.api_core import exceptions as gcs_exceptions

    try:
        gcs = _get_gcs_client()
        if not gcs:
            return None
        blob = gcs.bucket(GCS_BUCKET_NAME).blob(_job_gcs_name(job_id))
        if not blob.exists():
            return None
        blob.reload()
        current = _decode_job_payload(blob.download_as_text())
        if not current:
            return None
        status = current.get("status")
        if status in {"succeeded", "failed"}:
            return None
        if status == "running" and not _lease_expired(current):
            return None
        if status not in {"pending", "running"}:
            return None
        claimed = _prepare_running_lease(current)
        generation = blob.generation
        blob.upload_from_string(
            json.dumps(claimed),
            content_type="application/json",
            if_generation_match=generation,
        )
        _write_local_job_cache(claimed)
        return claimed
    except gcs_exceptions.PreconditionFailed:
        return None
    except Exception as e:
        print(f"Warning: Failed to claim GCS job {job_id}: {e}")
        return None


def claim_ingest_job(job_id: str) -> dict | None:
    """pending→running (or expired running) with CAS / generation match."""
    if job_id in _ingest_in_flight:
        return None
    if GCS_BUCKET_NAME:
        return _claim_gcs_job(job_id)
    with _job_write_lock:
        return _claim_local_job(job_id)


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


def list_pending_ingest_jobs() -> list[dict]:
    """List pending jobs. GCS is source of truth when GCS_BUCKET_NAME is set."""
    seen: dict[str, dict] = {}
    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                bucket = gcs.bucket(GCS_BUCKET_NAME)
                for blob in bucket.list_blobs(prefix=JOBS_GCS_PREFIX):
                    if not _is_job_json_name(blob.name.rsplit("/", 1)[-1]):
                        continue
                    job = _decode_job_payload(blob.download_as_text())
                    if _is_drainable(job):
                        seen[job["job_id"]] = job
        except Exception as e:
            print(f"Warning: Failed to list GCS ingest jobs: {e}")
            return _list_local_pending_jobs()
        return list(seen.values())

    return _list_local_pending_jobs()


def _list_local_pending_jobs() -> list[dict]:
    seen: dict[str, dict] = {}
    jobs_dir = os.path.join(MEDIA_DIR, JOBS_DIR_NAME)
    try:
        os.makedirs(jobs_dir, exist_ok=True)
        for name in os.listdir(jobs_dir):
            if not _is_job_json_name(name):
                continue
            try:
                with open(os.path.join(jobs_dir, name)) as f:
                    job = _decode_job_payload(f.read())
            except Exception as e:
                print(f"Warning: Failed to read local job {name}: {e}")
                continue
            if _is_drainable(job):
                seen[job["job_id"]] = job
    except Exception as e:
        print(f"Warning: Failed to list local ingest jobs: {e}")
    return list(seen.values())


def job_public_view(job: dict) -> dict:
    status = job["status"]
    if status == "running":
        status = "pending"
    view = {
        "job_id": job["job_id"],
        "status": status,
        "image_path": job.get("image_path"),
    }
    if job.get("status") == "succeeded":
        for key in ("summary", "reply", "merchant", "amount", "currency", "date"):
            view[key] = job.get(key)
    if job.get("status") == "failed":
        view["error"] = job.get("error") or "ingest_failed"
    return view


def _mark_job_failed(job: dict, error: str) -> dict:
    job["status"] = "failed"
    job["error"] = error
    try:
        write_job_record(job)
    except EnqueueError as write_exc:
        print(f"Warning: Failed to persist failed job {job['job_id']}: {write_exc}")
    return job


async def _commit_succeeded_job(job: dict) -> dict:
    """Persist store-commit then the job. Retry the job write, never extract/store."""
    job["status"] = "succeeded"
    job["error"] = None
    job["store_committed"] = True
    job["_durable_written"] = False
    try:
        _write_store_commit(job)
    except EnqueueError as write_exc:
        print(f"Warning: durable store-commit failed for {job['job_id']}: {write_exc}")
        return job
    try:
        write_job_record(job)
        job["_durable_written"] = True
    except EnqueueError as write_exc:
        print(
            f"Warning: ingest succeeded but job record persist failed "
            f"for {job['job_id']}: {write_exc}"
        )
    return job


async def process_ingest_job(job: dict) -> dict:
    """Run extract+store for one persisted job. Updates the durable record."""
    job_id = job["job_id"]
    committed = _load_store_commit(job_id)
    if committed and committed.get("store_committed"):
        return await _commit_succeeded_job(committed)
    if job_id in _ingest_in_flight:
        return job
    claimed = claim_ingest_job(job_id)
    if claimed is None:
        return load_job(job_id) or job
    _ingest_in_flight.add(job_id)
    try:
        data = load_uploaded_image_bytes(claimed["image_name"])
        if not data:
            return _mark_job_failed(claimed, "image_missing")
        try:
            reply_text = await ingest_uploaded_image(
                data,
                claimed["filename"],
                claimed["media_type"],
                claimed.get("image_path") or claimed.get("protected_url"),
                claimed.get("subject"),
            )
        except Exception as exc:
            if is_transient_ingest_error(exc):
                print(f"Warning: transient ingest error for {job_id}: {exc}")
                claimed["status"] = "pending"
                claimed["lease_until"] = None
                try:
                    write_job_record(claimed)
                except EnqueueError as write_exc:
                    print(f"Warning: failed to release lease for {job_id}: {write_exc}")
                return claimed
            print(f"Error: ingest job {job_id} failed: {exc}")
            return _mark_job_failed(claimed, "ingest_failed")
        fields = _receipt_response_fields(reply_text)
        claimed.update(fields)
        return await _commit_succeeded_job(claimed)
    finally:
        _ingest_in_flight.discard(job_id)


def _list_store_commit_jobs() -> list[dict]:
    jobs: list[dict] = []
    seen: set[str] = set()
    if GCS_BUCKET_NAME:
        try:
            gcs = _get_gcs_client()
            if gcs:
                for blob in gcs.bucket(GCS_BUCKET_NAME).list_blobs(
                    prefix=JOBS_GCS_PREFIX
                ):
                    name = blob.name.rsplit("/", 1)[-1]
                    if not name.endswith(".stored.json"):
                        continue
                    mark = _decode_job_payload(blob.download_as_text())
                    if mark and mark.get("store_committed"):
                        jobs.append(mark)
                        seen.add(mark["job_id"])
        except Exception as e:
            print(f"Warning: Failed to list GCS store-commits: {e}")
    jobs_dir = os.path.join(MEDIA_DIR, JOBS_DIR_NAME)
    if os.path.isdir(jobs_dir):
        for name in os.listdir(jobs_dir):
            if not name.endswith(".stored.json"):
                continue
            job_id = name[: -len(".stored.json")]
            if job_id in seen:
                continue
            mark = _load_local_store_commit(job_id)
            if mark and mark.get("store_committed"):
                jobs.append(mark)
    return jobs


async def drain_pending_ingest_jobs(limit: int | None = None) -> list[str]:
    """Process pending/expired-running jobs and retry store-commits."""
    completed: list[str] = []
    by_id = {job["job_id"]: job for job in list_pending_ingest_jobs()}
    for mark in _list_store_commit_jobs():
        by_id.setdefault(mark["job_id"], mark)
    jobs = list(by_id.values())
    if limit is not None:
        jobs = jobs[:limit]
    for job in jobs:
        updated = await process_ingest_job(job)
        job_id = updated["job_id"]
        if _durable_job_terminal(job_id):
            completed.append(job_id)
    return completed


async def _run_ingest_drain_worker() -> None:
    """Local uvicorn only. Production ingest is Cloud Tasks → POST /ingest."""
    interval = INGEST_DRAIN_INTERVAL_SEC
    if interval <= 0:
        return
    while True:
        try:
            await drain_pending_ingest_jobs()
        except Exception as exc:
            print(f"Error: ingest drain worker failed: {exc}")
        await asyncio.sleep(interval)


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
):
    """Persist + enqueue only. All clients get 202; ingest is not in this request."""
    verify_api_key(req, x_api_key)
    raw_bytes = await file.read()
    raw_filename = file.filename or "uploaded_photo.jpg"

    file_bytes, filename, media_type = normalize_image(
        raw_bytes, raw_filename, file.content_type
    )

    image_name = f"{uuid.uuid4()}_{filename}"
    try:
        protected_url = persist_uploaded_image(file_bytes, image_name, media_type)
        job = enqueue_ingest_job(
            image_name, filename, media_type, protected_url, subject
        )
    except (PersistError, EnqueueError) as exc:
        print(f"Error: upload persist/enqueue failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to persist upload") from exc

    return JSONResponse(
        {
            "status": "accepted",
            "job_id": job["job_id"],
            "image_path": protected_url,
        },
        status_code=202,
    )


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    req: Request,
    x_api_key: Annotated[str | None, Header()] = None,
):
    """Poll ingest status. Auth required. Unknown ids are 404."""
    verify_api_key(req, x_api_key)
    parsed = parse_job_id(job_id)
    job = load_job(parsed)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(job_public_view(job))


@app.api_route("/media/{image_name}", methods=["GET", "HEAD"])
async def get_media(
    image_name: str, req: Request, x_api_key: str | None = Header(None)
):
    """Authenticated image retrieval endpoint requiring API Key verification."""
    verify_api_key(req, x_api_key)

    # Reject any name that is not a bare filename (path traversal defense).
    if os.path.basename(image_name) != image_name or image_name in ("", ".", ".."):
        raise HTTPException(status_code=404, detail="Image not found")

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
    return JSONResponse({"status": "ok"})


@app.post("/ingest")
async def ingest_pending(
    req: Request, x_api_key: Annotated[str | None, Header()] = None
):
    """Cloud Tasks / worker target. Shortcut clients must not call this."""
    verify_api_key(req, x_api_key)
    payload: dict = {}
    try:
        body = await req.json()
        if isinstance(body, dict):
            payload = body
    except Exception:
        payload = {}
    job_id = payload.get("job_id")
    if job_id:
        parsed = parse_job_id(str(job_id))
        record = _load_job_record(parsed)
        mark = _load_store_commit(parsed)
        if not record and not mark:
            raise HTTPException(status_code=404, detail="Job not found")
        if _durable_job_terminal(parsed):
            return JSONResponse({"status": "ok", "completed": [parsed]})
        job = record or mark
        await process_ingest_job(job)
        if _durable_job_terminal(parsed):
            return JSONResponse({"status": "ok", "completed": [parsed]})
        return JSONResponse(
            {"status": "retry", "completed": []},
            status_code=503,
        )
    completed = await drain_pending_ingest_jobs(limit=1)
    if completed:
        return JSONResponse({"status": "ok", "completed": completed})
    record_ids = [
        job["job_id"]
        for job in list_pending_ingest_jobs()
        if _load_store_commit(job["job_id"])
    ]
    if record_ids or _list_store_commit_jobs():
        return JSONResponse({"status": "retry", "completed": []}, status_code=503)
    return JSONResponse({"status": "ok", "completed": completed})


static_dir = "static" if os.path.exists("static") else "frontend/static"
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
