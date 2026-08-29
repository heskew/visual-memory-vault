"""CI integration tests for persist + enqueue + 202 + GET /jobs poll.

Hits the real FastAPI proxy over HTTP. Extract/A2A is stubbed at
ingest_uploaded_image so CI needs no Gemini or Flair; assertions are
real status codes, bodies, on-disk jobs, and timing.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from a2a.client import A2AClientError, AgentCardResolutionError
from httpx import ASGITransport, AsyncClient
from PIL import Image

from frontend.main import EnqueueError, PersistError, drain_pending_ingest_jobs


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), color=(40, 90, 160)).save(buf, format="JPEG")
    return buf.getvalue()


def _a2a_http_error(status_code: int) -> A2AClientError:
    request = httpx.Request("POST", "http://127.0.0.1/a2a")
    response = httpx.Response(status_code, request=request)
    http_err = httpx.HTTPStatusError("a2a http", request=request, response=response)
    exc = A2AClientError(f"HTTP Error {status_code}: {http_err}")
    exc.__cause__ = http_err
    return exc


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.API_KEY", "ci-secret")
    return tmp_path


@pytest_asyncio.fixture(loop_scope="function")
async def client(proxy_env):
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Api-Key": "ci-secret"},
    ) as ac:
        yield ac


async def _upload(client: AsyncClient, name: str = "receipt.jpg") -> httpx.Response:
    return await client.post(
        "/upload",
        files={"file": (name, _jpeg_bytes(), "image/jpeg")},
        data={"subject": name},
    )


@pytest.mark.asyncio
async def test_upload_202_requires_persist_and_enqueue(client, proxy_env, monkeypatch):
    ingest_calls = {"n": 0}

    async def forbidden(*args, **kwargs):
        ingest_calls["n"] += 1
        raise AssertionError("upload must not run extract")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", forbidden)

    response = await _upload(client)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"status", "job_id", "image_path"}
    assert body["status"] == "accepted"
    job_uuid = uuid.UUID(body["job_id"])
    assert job_uuid.version == 4
    assert body["image_path"].startswith("/media/")
    assert "summary" not in body
    assert "reply" not in body
    assert ingest_calls["n"] == 0

    job_path = proxy_env / "jobs" / f"{job_uuid}.json"
    assert job_path.is_file()
    stored = json.loads(job_path.read_text())
    assert stored["status"] == "pending"
    image = proxy_env / Path(body["image_path"]).name
    assert image.is_file()
    assert image.stat().st_size > 0

    def persist_boom(*args, **kwargs):
        raise PersistError("disk full")

    monkeypatch.setattr("frontend.main.persist_uploaded_image", persist_boom)
    persist_fail = await _upload(client)
    assert persist_fail.status_code == 500
    assert persist_fail.json()["detail"] == "Failed to persist upload"

    monkeypatch.setattr(
        "frontend.main.persist_uploaded_image", lambda *a, **k: "/media/ok.jpg"
    )

    def enqueue_boom(*args, **kwargs):
        raise EnqueueError("job store down")

    monkeypatch.setattr("frontend.main.enqueue_ingest_job", enqueue_boom)
    enqueue_fail = await _upload(client)
    assert enqueue_fail.status_code == 500
    assert enqueue_fail.json()["detail"] == "Failed to persist upload"


@pytest.mark.asyncio
async def test_jobs_poll_pending_succeeded_failed_and_auth(client, monkeypatch):
    async def succeed(*args, **kwargs):
        return (
            "Saved dinner at Joe's Grill.\n"
            'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
        )

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", succeed)
    uploaded = await _upload(client, "dinner.jpg")
    assert uploaded.status_code == 202
    job_id = uploaded.json()["job_id"]

    pending = await client.get(f"/jobs/{job_id}")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"
    assert "summary" not in pending.json()

    ingest = await client.post("/ingest")
    assert ingest.status_code == 200
    assert ingest.json()["completed"] == [job_id]

    done = await client.get(f"/jobs/{job_id}")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "succeeded"
    assert body["summary"] == "Saved dinner at Joe's Grill."
    assert body["merchant"] == "Joe's Grill"
    assert body["amount"] == "58.40"

    async def a2a_400(*args, **kwargs):
        raise _a2a_http_error(400)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_400)
    failed_upload = await _upload(client, "bad.jpg")
    fail_id = failed_upload.json()["job_id"]
    await client.post("/ingest")
    failed = await client.get(f"/jobs/{fail_id}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error"] == "ingest_failed"

    assert (await client.get("/jobs/not-a-uuid")).status_code == 404
    assert (
        await client.get("/jobs/11111111-1111-1111-1111-111111111111")
    ).status_code == 404

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        denied = await anon.get(f"/jobs/{job_id}")
        assert denied.status_code == 401
        wrong = await anon.get(f"/jobs/{job_id}", headers={"X-Api-Key": "wrong-secret"})
        assert wrong.status_code == 401
        upload_denied = await anon.post(
            "/upload",
            files={"file": ("nope.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        assert upload_denied.status_code == 401


@pytest.mark.asyncio
async def test_upload_returns_202_before_hung_extract(client, monkeypatch):
    started = asyncio.Event()

    async def hung(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)
        return "should never reach the upload client"

    async def hung_process(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", hung)
    monkeypatch.setattr("frontend.main.process_ingest_job", hung_process)
    t0 = time.monotonic()
    response = await asyncio.wait_for(_upload(client), timeout=3)
    elapsed = time.monotonic() - t0
    assert response.status_code == 202
    assert elapsed < 1.5
    await asyncio.sleep(0.05)
    assert not started.is_set()
    job_id = response.json()["job_id"]
    poll = await client.get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_a2a_4xx_is_terminal_and_not_retried(client, monkeypatch):
    calls = {"n": 0}

    async def a2a_400(*args, **kwargs):
        calls["n"] += 1
        raise _a2a_http_error(400)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_400)
    job_id = (await _upload(client, "four.jpg")).json()["job_id"]
    first = await client.post("/ingest")
    assert first.status_code == 200
    assert first.json()["completed"] == [job_id]
    assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "failed"
    assert calls["n"] == 1

    second = await client.post("/ingest")
    assert second.json()["completed"] == []
    assert calls["n"] == 1

    async def a2a_403(*args, **kwargs):
        calls["n"] += 1
        raise _a2a_http_error(403)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_403)
    forbid_id = (await _upload(client, "forbid.jpg")).json()["job_id"]
    before_403 = calls["n"]
    await client.post("/ingest")
    assert (await client.get(f"/jobs/{forbid_id}")).json()["status"] == "failed"
    await client.post("/ingest")
    assert calls["n"] == before_403 + 1

    async def card_404(*args, **kwargs):
        calls["n"] += 1
        raise AgentCardResolutionError("missing card", status_code=404)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", card_404)
    card_id = (await _upload(client, "card.jpg")).json()["job_id"]
    before_card = calls["n"]
    await client.post("/ingest")
    assert (await client.get(f"/jobs/{card_id}")).json()["status"] == "failed"
    await client.post("/ingest")
    assert calls["n"] == before_card + 1


@pytest.mark.asyncio
async def test_a2a_429_and_5xx_stay_pending_and_retry(client, monkeypatch):
    calls = {"n": 0}

    async def a2a_429(*args, **kwargs):
        calls["n"] += 1
        raise _a2a_http_error(429)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_429)
    retry_id = (await _upload(client, "retry.jpg")).json()["job_id"]
    first = await client.post("/ingest")
    assert first.status_code == 200
    assert first.json()["completed"] == []
    assert (await client.get(f"/jobs/{retry_id}")).json()["status"] == "pending"
    assert calls["n"] == 1
    second = await client.post("/ingest")
    assert second.json()["completed"] == []
    assert (await client.get(f"/jobs/{retry_id}")).json()["status"] == "pending"
    assert calls["n"] == 2

    async def a2a_503(*args, **kwargs):
        calls["n"] += 1
        raise _a2a_http_error(503)

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", a2a_503)
    five_id = (await _upload(client, "five.jpg")).json()["job_id"]
    # Leftover 429 is still pending; drain all so the 503 job is actually attempted.
    drained = await drain_pending_ingest_jobs()
    assert drained == []
    assert (await client.get(f"/jobs/{five_id}")).json()["status"] == "pending"
    assert (await client.get(f"/jobs/{retry_id}")).json()["status"] == "pending"
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_get_jobs_uses_local_cache_when_gcs_read_throws(
    proxy_env, client, monkeypatch
):
    uploaded = await _upload(client, "cache.jpg")
    job_id = uploaded.json()["job_id"]
    assert (proxy_env / "jobs" / f"{job_id}.json").is_file()

    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "existing-bucket")

    def gcs_boom():
        raise RuntimeError("GCS unavailable")

    monkeypatch.setattr("frontend.main._get_gcs_client", gcs_boom)
    response = await client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["job_id"] == job_id
    assert response.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_web_client_uploads_then_polls_jobs_no_wait_flag(proxy_env, monkeypatch):
    html = Path("frontend/static/index.html").read_text()
    js = Path("frontend/static/vault-upload.js").read_text()
    assert "wait=1" not in html
    assert "wait=1" not in js
    assert "VaultUpload.uploadPhoto" in html
    assert "VaultUpload.pollJob(accepted.job_id" in html
    assert 'fetchImpl("/jobs/"' in js or 'fetchImpl("/jobs/" +' in js

    async def succeed(*args, **kwargs):
        return (
            "Saved dinner at Joe's Grill.\n"
            'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
        )

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", succeed)

    from frontend.main import app

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                ping = httpx.get(f"{base}/health", timeout=0.3)
                if ping.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            pytest.fail("live proxy did not start")

        upload_file = proxy_env / "client-receipt.jpg"
        upload_file.write_bytes(_jpeg_bytes())
        env = {
            **os.environ,
            "PROXY_BASE": base,
            "PROXY_API_KEY": "ci-secret",
            "UPLOAD_FILE": str(upload_file),
        }
        uploaded = subprocess.run(
            ["node", "tests/integration/vault_upload_live.js", "upload"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert uploaded.returncode == 0, uploaded.stdout + uploaded.stderr
        accepted = json.loads(uploaded.stdout.strip().splitlines()[-1])
        assert accepted["status"] == "accepted"
        job_id = accepted["job_id"]
        assert uuid.UUID(job_id).version == 4
        assert "summary" not in accepted

        still_pending = subprocess.run(
            ["node", "tests/integration/vault_upload_live.js", "poll-timeout", job_id],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert still_pending.returncode == 0, (
            still_pending.stdout + still_pending.stderr
        )
        assert (
            json.loads(still_pending.stdout.strip().splitlines()[-1])["timed_out"]
            is True
        )
        pending_http = httpx.get(
            f"{base}/jobs/{job_id}",
            headers={"X-Api-Key": "ci-secret"},
            timeout=2,
        )
        assert pending_http.status_code == 200
        assert pending_http.json()["status"] == "pending"

        drained = await drain_pending_ingest_jobs(limit=1)
        assert job_id in drained

        polled = subprocess.run(
            ["node", "tests/integration/vault_upload_live.js", "poll", job_id],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert polled.returncode == 0, polled.stdout + polled.stderr
        job = json.loads(polled.stdout.strip().splitlines()[-1])
        assert job["status"] == "succeeded"
        assert job["merchant"] == "Joe's Grill"
        assert job["summary"] == "Saved dinner at Joe's Grill."
    finally:
        server.should_exit = True
        thread.join(timeout=3)
