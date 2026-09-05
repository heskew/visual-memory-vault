import asyncio
import io
import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from a2a.client import A2AClientError
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.requests import Request

from frontend.main import (
    JOBS_GCS_PREFIX,
    EnqueueError,
    PersistError,
    TerminalIngestError,
    _extract_parts,
    _load_job_record,
    claim_ingest_job,
    drain_pending_ingest_jobs,
    enqueue_ingest_job,
    extract_receipt_fields,
    is_transient_ingest_error,
    list_pending_ingest_jobs,
    load_job,
    persist_uploaded_image,
    process_ingest_job,
    strip_receipt_marker,
    verify_api_key,
    write_job_record,
)


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(200, 10, 10)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _open_proxy_for_functional_tests(monkeypatch):
    # Most tests exercise proxy endpoints without a configured PROXY_API_KEY.
    # Default them into the explicit local-dev open mode so auth does not block
    # them; the auth-specific tests override this.
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", True)


def test_verify_api_key_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.setattr("frontend.main.API_KEY", "")
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", False)
    scope = {"type": "http", "headers": [], "query_string": b""}
    req = Request(scope)
    with pytest.raises(HTTPException) as exc:
        verify_api_key(req)
    assert exc.value.status_code == 503

    # Explicit local-dev opt-in re-opens the proxy.
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", True)
    assert verify_api_key(req) is True


def test_verify_api_key_when_configured(monkeypatch):
    monkeypatch.setattr("frontend.main.API_KEY", "secret-key")
    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"secret-key")],
        "query_string": b"",
    }
    req = Request(scope)
    assert verify_api_key(req) is True

    bad_scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"wrong-key")],
        "query_string": b"",
    }
    bad_req = Request(bad_scope)
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(bad_req)
    assert exc_info.value.status_code == 401


def test_extract_parts():
    parts = [{"text": "Hello world"}, {"text": "How can I help?"}]
    extracted = _extract_parts(parts)
    assert len(extracted) == 2
    assert extracted[0] == {"kind": "text", "text": "Hello world"}


def test_extract_receipt_fields_from_marker_line():
    text = (
        "Saved dinner at Joe's Grill.\n"
        'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
    )
    fields = extract_receipt_fields(text)
    assert fields["merchant"] == "Joe's Grill"
    assert fields["amount"] == "58.40"
    assert fields["currency"] == "USD"
    assert fields["date"] == "2026-08-20"
    assert "RECEIPT:" not in strip_receipt_marker(text)
    assert "Saved dinner at Joe's Grill." in strip_receipt_marker(text)


def test_extract_and_strip_receipt_marker_with_trailing_period():
    text = (
        "Saved dinner at Joe's Grill.\n"
        'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}.'
    )
    fields = extract_receipt_fields(text)
    assert fields["merchant"] == "Joe's Grill"
    assert fields["amount"] == "58.40"
    assert fields["currency"] == "USD"
    assert fields["date"] == "2026-08-20"
    stripped = strip_receipt_marker(text)
    assert "RECEIPT:" not in stripped
    assert "Saved dinner at Joe's Grill." in stripped


def test_extract_receipt_fields_empty_when_not_a_receipt():
    fields = extract_receipt_fields("Saved hotel WiFi card. Network: Guest.")
    assert fields == {
        "merchant": None,
        "amount": None,
        "currency": None,
        "date": None,
    }


def test_index_html_has_three_row_receipt_chip():
    html = Path("frontend/static/index.html").read_text()
    assert "receipt-chip" in html
    assert "receipt-row merchant" in html
    assert "receipt-row amount" in html
    assert "receipt-row date" in html
    assert "function receiptChip" in html
    assert "wait=1" not in html
    assert "vault-upload.js" in html
    assert "pollJob" in html
    assert "function vaultFetch" in html
    assert 'headers["X-Api-Key"]' in html


def test_web_ui_polls_jobs_not_wait_flag():
    html = Path("frontend/static/index.html").read_text()
    js = Path("frontend/static/vault-upload.js").read_text()
    assert "wait=1" not in html
    assert "wait=1" not in js
    assert 'fetchImpl("/upload"' in js or 'fetchImpl("/upload",' in js
    assert "/jobs/" in js
    assert "pollJob" in js
    assert "VaultUpload.uploadPhoto(file, vaultFetch)" in html
    assert "VaultUpload.pollJob(accepted.job_id, vaultFetch)" in html
    result = subprocess.run(
        ["node", "tests/unit/test_vault_upload.js"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_upload_returns_before_hung_agent(tmp_path, monkeypatch):
    """POST /upload must ack before A2A/Gemini; ingest is not in the request."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    ingest_called = False

    async def hung_ingest(*args, **kwargs):
        nonlocal ingest_called
        ingest_called = True
        await asyncio.sleep(3600)
        return "should not reach the shortcut client"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", hung_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.monotonic()
        response = await asyncio.wait_for(
            client.post(
                "/upload",
                files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
                data={"subject": "Dinner Receipt"},
            ),
            timeout=3,
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 202
        assert elapsed < 2
        body = response.json()
        assert body["status"] == "accepted"
        assert body["job_id"]
        assert body["image_path"].startswith("/media/")
        assert "summary" not in body
        assert "reply" not in body
        assert "merchant" not in body
        assert not ingest_called

        job = load_job(body["job_id"])
        assert job is not None
        assert job["status"] == "pending"


@pytest.mark.asyncio
async def test_job_pollable_after_process_restart(tmp_path, monkeypatch):
    """Job records survive a simulated process restart (durable store, not RAM)."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/upload",
            files={"file": ("wifi.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        monkeypatch.setattr("frontend.main._card", None)

        pending = await client.get(f"/jobs/{job_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"
        assert pending.json()["job_id"] == job_id

        disk = json.loads((tmp_path / "jobs" / f"{job_id}.json").read_text())
        assert disk["status"] == "pending"

        async def fake_ingest(*args, **kwargs):
            return (
                "Saved hotel WiFi card.\n"
                'RECEIPT: {"merchant":"Hotel","amount":"0","currency":"USD","date":"2026-08-29"}'
            )

        monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)
        completed = await drain_pending_ingest_jobs()
        assert job_id in completed

        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        done = await client.get(f"/jobs/{job_id}")
        assert done.status_code == 200
        body = done.json()
        assert body["status"] == "succeeded"
        assert body["summary"] == "Saved hotel WiFi card."
        assert body["merchant"] == "Hotel"


@pytest.mark.asyncio
async def test_get_job_requires_auth_and_unknown_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.API_KEY", "secret-key")

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/jobs/11111111-1111-1111-1111-111111111111")
        assert denied.status_code == 401

        missing = await client.get(
            "/jobs/11111111-1111-1111-1111-111111111111",
            headers={"X-Api-Key": "secret-key"},
        )
        assert missing.status_code == 404

        bogus = await client.get(
            "/jobs/not-a-uuid",
            headers={"X-Api-Key": "secret-key"},
        )
        assert bogus.status_code == 404


@pytest.mark.asyncio
async def test_persist_or_enqueue_failure_is_not_202(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:

        def persist_boom(*args, **kwargs):
            raise PersistError("disk full")

        monkeypatch.setattr("frontend.main.persist_uploaded_image", persist_boom)
        persist_fail = await client.post(
            "/upload",
            files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert persist_fail.status_code != 202

        monkeypatch.setattr(
            "frontend.main.persist_uploaded_image",
            lambda *a, **k: "/media/ok.jpg",
        )

        def enqueue_boom(*args, **kwargs):
            raise EnqueueError("job store down")

        monkeypatch.setattr("frontend.main.enqueue_ingest_job", enqueue_boom)
        enqueue_fail = await client.post(
            "/upload",
            files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert enqueue_fail.status_code != 202


@pytest.mark.asyncio
async def test_drain_pending_ingest_jobs_calls_ingest(tmp_path, monkeypatch):
    """Drain loads a persisted job and calls ingest_uploaded_image (no live Flair)."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    image_name = "abc_shot.jpg"
    persist_uploaded_image(_jpeg_bytes(), image_name, "image/jpeg")
    job = enqueue_ingest_job(
        image_name, "shot.jpg", "image/jpeg", f"/media/{image_name}", "WiFi"
    )

    calls = []

    async def fake_ingest(file_bytes, filename, media_type, protected_url, subject):
        calls.append(
            {
                "filename": filename,
                "media_type": media_type,
                "protected_url": protected_url,
                "subject": subject,
                "nbytes": len(file_bytes),
            }
        )
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)
    completed = await drain_pending_ingest_jobs()

    assert completed == [job["job_id"]]
    assert len(calls) == 1
    assert calls[0]["filename"] == "shot.jpg"
    assert calls[0]["subject"] == "WiFi"
    stored = load_job(job["job_id"])
    assert stored["status"] == "succeeded"
    assert stored["summary"] == "stored"


@pytest.mark.asyncio
async def test_ingest_endpoint_runs_one_persisted_job(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    image_name = "xyz_menu.jpg"
    persist_uploaded_image(_jpeg_bytes(), image_name, "image/jpeg")
    job = enqueue_ingest_job(
        image_name, "menu.jpg", "image/jpeg", f"/media/{image_name}", "Menu"
    )

    calls = []

    async def fake_ingest(*args, **kwargs):
        calls.append(args)
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ingest")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "completed": [job["job_id"]]}
    assert len(calls) == 1
    assert load_job(job["job_id"])["status"] == "succeeded"


class _FakeBlob:
    def __init__(self, name: str, data: str | None = None):
        self.name = name
        self._data = data
        self.generation = 1 if data is not None else 0

    def exists(self) -> bool:
        return self._data is not None

    def reload(self) -> None:
        return None

    def download_as_text(self) -> str:
        return self._data or ""

    def upload_from_string(
        self,
        payload: str,
        content_type: str | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        current_gen = self.generation if self._data is not None else 0
        if if_generation_match is not None and current_gen != if_generation_match:
            from google.api_core.exceptions import PreconditionFailed

            raise PreconditionFailed("generation mismatch")
        self._data = payload
        self.generation = current_gen + 1


class _FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        if name not in self.blobs:
            self.blobs[name] = _FakeBlob(name)
        return self.blobs[name]

    def list_blobs(self, prefix: str = ""):
        return [
            blob
            for name, blob in self.blobs.items()
            if name.startswith(prefix) and blob.exists()
        ]


class _FakeGCS:
    def __init__(self) -> None:
        self._bucket = _FakeBucket()

    def bucket(self, name: str) -> _FakeBucket:
        return self._bucket


def _job_record(job_id: str, status: str) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "image_name": "shot.jpg",
        "filename": "shot.jpg",
        "media_type": "image/jpeg",
        "image_path": "/media/shot.jpg",
        "subject": "WiFi",
        "summary": "Saved" if status == "succeeded" else None,
        "reply": "Saved" if status == "succeeded" else None,
        "merchant": None,
        "amount": None,
        "currency": None,
        "date": None,
        "error": None,
    }


@pytest.mark.asyncio
async def test_transient_ingest_error_stays_pending_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    attempts = {"n": 0}

    async def flaky_ingest(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", flaky_ingest)
    first = await drain_pending_ingest_jobs()
    assert first == []
    assert load_job(job["job_id"])["status"] == "pending"

    second = await drain_pending_ingest_jobs()
    assert second == [job["job_id"]]
    assert load_job(job["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_terminal_ingest_error_marks_job_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )

    async def bad_request(*args, **kwargs):
        request = httpx.Request("POST", "http://127.0.0.1/a2a")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", bad_request)
    completed = await drain_pending_ingest_jobs()
    assert completed == [job["job_id"]]
    stored = load_job(job["job_id"])
    assert stored["status"] == "failed"
    assert stored["error"] == "ingest_failed"

    async def never(*args, **kwargs):
        raise AssertionError("terminal jobs must not be retried")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", never)
    assert await drain_pending_ingest_jobs() == []


@pytest.mark.asyncio
async def test_terminal_ingest_error_class_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )

    async def terminal(*args, **kwargs):
        raise TerminalIngestError("permanent")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", terminal)
    await process_ingest_job(job)
    assert load_job(job["job_id"])["status"] == "failed"


def test_gcs_is_source_of_truth_over_stale_local(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    gcs = _FakeGCS()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)

    job_id = "11111111-1111-4111-8111-111111111111"
    local_pending = _job_record(job_id, "pending")
    (tmp_path / "jobs").mkdir(parents=True)
    (tmp_path / "jobs" / f"{job_id}.json").write_text(json.dumps(local_pending))

    gcs_succeeded = _job_record(job_id, "succeeded")
    gcs.bucket("existing-bucket").blob(
        f"{JOBS_GCS_PREFIX}{job_id}.json"
    ).upload_from_string(json.dumps(gcs_succeeded))

    loaded = load_job(job_id)
    assert loaded is not None
    assert loaded["status"] == "succeeded"
    assert list_pending_ingest_jobs() == []


@pytest.mark.asyncio
async def test_successful_store_stays_succeeded_if_job_write_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    job_id = job["job_id"]
    ingest_calls = {"n": 0}

    async def fake_ingest(*args, **kwargs):
        ingest_calls["n"] += 1
        return "stored"

    writes = {"n": 0}

    def write_after_success(record):
        writes["n"] += 1
        path = tmp_path / "jobs" / f"{record['job_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if record.get("status") == "succeeded" and writes["n"] == 1:
            raise EnqueueError("durable job write failed")
        path.write_text(json.dumps(record))

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)
    monkeypatch.setattr("frontend.main.write_job_record", write_after_success)
    updated = await process_ingest_job(job)
    assert updated["status"] == "succeeded"
    assert updated["summary"] == "stored"
    assert updated.get("error") is None
    durable = json.loads((tmp_path / "jobs" / f"{job_id}.json").read_text())
    assert durable["status"] != "succeeded"
    assert (tmp_path / "jobs" / f"{job_id}.stored.json").is_file()

    async def never(*args, **kwargs):
        raise AssertionError("store_memory must not run again")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", never)
    second = await drain_pending_ingest_jobs()
    assert job_id in second
    assert ingest_calls["n"] == 1
    assert load_job(job_id)["status"] == "succeeded"


def _a2a_http_error(status_code: int) -> A2AClientError:
    """Match a2a.client.transports.http_helpers.handle_http_exceptions."""
    request = httpx.Request("POST", "http://127.0.0.1/a2a")
    response = httpx.Response(status_code, request=request)
    http_err = httpx.HTTPStatusError("bad request", request=request, response=response)
    exc = A2AClientError(f"HTTP Error {status_code}: {http_err}")
    exc.__cause__ = http_err
    return exc


@pytest.mark.asyncio
async def test_a2a_http_400_marks_job_failed_and_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )

    async def a2a_400(*args, **kwargs):
        raise _a2a_http_error(400)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_400)
    completed = await drain_pending_ingest_jobs()
    assert completed == [job["job_id"]]
    stored = load_job(job["job_id"])
    assert stored["status"] == "failed"
    assert stored["error"] == "ingest_failed"

    async def never(*args, **kwargs):
        raise AssertionError("A2A 4xx jobs must not be retried")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", never)
    assert await drain_pending_ingest_jobs() == []


@pytest.mark.asyncio
async def test_get_job_falls_back_to_local_cache_when_gcs_read_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    job_id = "22222222-2222-4222-8222-222222222222"
    cached = _job_record(job_id, "pending")
    (tmp_path / "jobs").mkdir(parents=True)
    (tmp_path / "jobs" / f"{job_id}.json").write_text(json.dumps(cached))

    def gcs_boom():
        raise RuntimeError("GCS unavailable")

    monkeypatch.setattr("frontend.main._get_gcs_client", gcs_boom)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["status"] == "pending"


def test_programmer_errors_are_terminal_not_retried():
    assert is_transient_ingest_error(TypeError("bug")) is False
    assert is_transient_ingest_error(KeyError("missing")) is False
    assert is_transient_ingest_error(AttributeError("nope")) is False
    assert is_transient_ingest_error(httpx.ConnectError("refused")) is True


def test_enqueue_requires_cloud_tasks_when_gcs_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", None)
    gcs = _FakeGCS()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    with pytest.raises(EnqueueError, match="CLOUD_TASKS_QUEUE"):
        enqueue_ingest_job(
            "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
        )


def test_enqueue_creates_cloud_task_for_new_ingest_request(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    created = []
    monkeypatch.setattr(
        "frontend.main.create_ingest_cloud_task",
        lambda job_id: created.append(job_id),
    )
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    assert created == [job["job_id"]]
    assert load_job(job["job_id"])["status"] == "pending"


def test_gcs_list_failure_falls_back_to_local_pending(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    job_id = "33333333-3333-4333-8333-333333333333"
    (tmp_path / "jobs").mkdir(parents=True)
    (tmp_path / "jobs" / f"{job_id}.json").write_text(
        json.dumps(_job_record(job_id, "pending"))
    )

    def gcs_boom():
        raise RuntimeError("GCS list unavailable")

    monkeypatch.setattr("frontend.main._get_gcs_client", gcs_boom)
    pending = list_pending_ingest_jobs()
    assert len(pending) == 1
    assert pending[0]["job_id"] == job_id


@pytest.mark.asyncio
async def test_lease_prevents_two_consumers_from_extracting(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_ingest(*args, **kwargs):
        calls["n"] += 1
        started.set()
        await release.wait()
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", slow_ingest)
    first = asyncio.create_task(process_ingest_job(dict(job)))
    await started.wait()
    second = await process_ingest_job(dict(job))
    assert second.get("status") == "running" or calls["n"] == 1
    assert claim_ingest_job(job["job_id"]) is None
    release.set()
    done = await first
    assert done["status"] == "succeeded"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_upload_stays_pending_until_separate_consumer_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    tasks = []
    monkeypatch.setattr(
        "frontend.main.create_ingest_cloud_task",
        lambda job_id: tasks.append(job_id),
    )

    async def fake_ingest(*args, **kwargs):
        return (
            "Saved dinner at Joe's Grill.\n"
            'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
        )

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uploaded = await client.post(
            "/upload",
            files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert uploaded.status_code == 202
        job_id = uploaded.json()["job_id"]
        assert tasks == [job_id]
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        pending = await client.get(f"/jobs/{job_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"

        consumer = await client.post("/ingest", json={"job_id": job_id})
        assert consumer.status_code == 200
        assert consumer.json()["completed"] == [job_id]
        done = await client.get(f"/jobs/{job_id}")
        assert done.json()["status"] == "succeeded"
        assert done.json()["merchant"] == "Joe's Grill"


@pytest.mark.asyncio
async def test_gcs_generation_match_blocks_second_claim(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr("frontend.main.create_ingest_cloud_task", lambda job_id: None)
    gcs = _FakeGCS()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    first = claim_ingest_job(job["job_id"])
    assert first is not None
    assert first["status"] == "running"
    assert claim_ingest_job(job["job_id"]) is None


@pytest.mark.asyncio
async def test_store_commit_is_durable_and_ingest_is_not_200_until_job_write(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr("frontend.main.create_ingest_cloud_task", lambda job_id: None)
    gcs = _FakeGCS()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    job_id = job["job_id"]
    ingest_calls = {"n": 0}

    async def fake_ingest(*args, **kwargs):
        ingest_calls["n"] += 1
        return "stored"

    from frontend.main import write_job_record as real_write

    fail_once = {"n": 0}

    def flaky_write(record):
        if record.get("status") == "succeeded" and fail_once["n"] == 0:
            fail_once["n"] += 1
            raise EnqueueError("durable job write failed")
        return real_write(record)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)
    monkeypatch.setattr("frontend.main.write_job_record", flaky_write)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/ingest", json={"job_id": job_id})
        assert first.status_code == 503
        assert _load_job_record(job_id)["status"] != "succeeded"
        assert (
            gcs.bucket("existing-bucket")
            .blob(f"{JOBS_GCS_PREFIX}{job_id}.stored.json")
            .exists()
        )
        polled = await client.get(f"/jobs/{job_id}")
        assert polled.status_code == 200
        assert polled.json()["status"] == "succeeded"

        for path in (tmp_path / "jobs").glob("*"):
            path.unlink()
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())

        async def never(*args, **kwargs):
            raise AssertionError("new instance must not re-extract")

        monkeypatch.setattr("frontend.main.ingest_uploaded_image", never)
        second = await client.post("/ingest", json={"job_id": job_id})
        assert second.status_code == 200
        assert ingest_calls["n"] == 1
        assert _load_job_record(job_id)["status"] == "succeeded"
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"


def test_stale_writer_cannot_overwrite_newer_lease_or_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr("frontend.main.create_ingest_cloud_task", lambda job_id: None)
    gcs = _FakeGCS()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    claimed = claim_ingest_job(job["job_id"])
    assert claimed is not None
    newer = dict(claimed)
    newer["lease_id"] = "newer-lease"
    newer["lease_until"] = time.time() + 120
    blob = gcs.bucket("existing-bucket").blob(f"{JOBS_GCS_PREFIX}{job['job_id']}.json")
    blob.upload_from_string(json.dumps(newer))
    stale = dict(claimed)
    stale["status"] = "pending"
    stale["lease_id"] = claimed["lease_id"]
    with pytest.raises(EnqueueError, match="stale"):
        write_job_record(stale)
    assert _load_job_record(job["job_id"])["lease_id"] == "newer-lease"

    terminal = _job_record(job["job_id"], "succeeded")
    terminal["lease_id"] = "newer-lease"
    write_job_record(terminal)
    stale_fail = dict(terminal)
    stale_fail["status"] = "failed"
    with pytest.raises(EnqueueError, match="stale"):
        write_job_record(stale_fail)
    assert _load_job_record(job["job_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_expired_running_job_is_reclaimed_without_job_id(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    persist_uploaded_image(_jpeg_bytes(), "shot.jpg", "image/jpeg")
    job = enqueue_ingest_job(
        "shot.jpg", "shot.jpg", "image/jpeg", "/media/shot.jpg", "WiFi"
    )
    running = dict(job)
    running["status"] = "running"
    running["lease_id"] = "dead-worker"
    running["lease_until"] = time.time() - 5
    write_job_record(running)
    listed = list_pending_ingest_jobs()
    assert listed and listed[0]["job_id"] == job["job_id"]
    assert listed[0]["status"] == "running"

    async def fake_ingest(*args, **kwargs):
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ingest")
        assert response.status_code == 200
        assert response.json()["completed"] == [job["job_id"]]
        assert (await client.get(f"/jobs/{job['job_id']}")).json()["status"] == (
            "succeeded"
        )
