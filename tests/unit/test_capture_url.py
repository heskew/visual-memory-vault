"""URL capture: scheme/address policy, 202 contract, and worker outcomes."""

from __future__ import annotations

import asyncio
import io
import json
import socket
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from frontend import capture as capture_mod
from frontend.capture import (
    CaptureRejected,
    CaptureTransient,
    capture_store_metadata,
    ensure_request_allowed,
    validate_capture_url,
)
from frontend.main import (
    _upload_ingest_prompt,
    load_job,
    persist_uploaded_image,
)

_REAL_RESOLVE = capture_mod.resolve_host

_PUBLIC = ["93.184.216.34"]


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 80, 140)).save(buf, format="JPEG")
    return buf.getvalue()


def _public(_host: str) -> list[str]:
    return list(_PUBLIC)


@pytest.fixture(autouse=True)
def _capture_env(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", None)
    monkeypatch.setattr("frontend.main.API_KEY", "")
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", True)
    monkeypatch.setattr("frontend.capture.resolve_host", _public)

    def forbid_render(url: str) -> bytes:
        raise AssertionError(f"render should not run for {url}")

    monkeypatch.setattr("frontend.capture.render_page_screenshot", forbid_render)
    return tmp_path


def test_capture_store_metadata_is_the_flair_shape():
    metadata = capture_store_metadata(
        "https://example.com/docs",
        "2026-09-27T18:00:00Z",
        "/media/page.jpg",
    )
    assert metadata == {
        "image_url": "/media/page.jpg",
        "source_url": "https://example.com/docs",
        "captured_at": "2026-09-27T18:00:00Z",
        "capture_kind": "url",
    }


def test_upload_prompt_has_no_capture_fields():
    prompt = _upload_ingest_prompt("shot.jpg", "/media/shot.jpg", "Dinner")
    assert "capture_kind" not in prompt
    assert "source_url" not in prompt


def test_capture_prompt_includes_flair_metadata():
    metadata = capture_store_metadata(
        "https://example.com/docs",
        "2026-09-27T18:04:00Z",
        "/media/page.jpg",
    )
    prompt = _upload_ingest_prompt("page.jpg", "/media/page.jpg", "Docs", metadata)
    assert json.dumps(metadata) in prompt
    assert "capture_kind" in prompt
    assert "source_url" in prompt
    assert "https://example.com/docs" in prompt
    assert "Page capture" not in prompt
    assert "Docs" in prompt
    bare = _upload_ingest_prompt("page.jpg", "/media/page.jpg", None, metadata)
    assert "Page capture" in bare


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/a", "unsupported_scheme"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("data:text/html,hi", "unsupported_scheme"),
        ("gopher://example.com/", "unsupported_scheme"),
        ("example.com", "bad_url"),
        ("", "bad_url"),
        ("http://", "bad_url"),
        ("http://example.com/a b", "bad_url"),
        ("http://" + ("a" * 3000) + ".example", "bad_url"),
        ("http://169.254.169.254/computeMetadata/v1/", "blocked_url"),
        ("http://169.254.169.254.nip.io/", "blocked_url"),
        ("http://metadata.google.internal/computeMetadata/v1/", "blocked_url"),
        ("http://metadata.google.com/", "blocked_url"),
        ("http://localhost/", "blocked_url"),
        ("http://foo.localhost/", "blocked_url"),
        ("http://127.0.0.1/", "blocked_url"),
        ("http://127.1/", "blocked_url"),
        ("http://2130706433/", "blocked_url"),
        ("http://0x7f000001/", "blocked_url"),
        ("http://0177.0.0.1/", "blocked_url"),
        ("http://10.1.2.3/", "blocked_url"),
        ("http://192.168.1.9/", "blocked_url"),
        ("http://172.16.5.5/", "blocked_url"),
        ("http://0.0.0.0/", "blocked_url"),
        ("http://[::1]/", "blocked_url"),
        ("http://[fe80::1]/", "blocked_url"),
        ("http://[::ffff:169.254.169.254]/", "blocked_url"),
        ("http://100.64.0.1/", "blocked_url"),
    ],
)
def test_terminal_capture_urls(url, reason):
    def resolver(host: str) -> list[str]:
        if host.endswith(".nip.io"):
            return ["169.254.169.254"]
        return list(_PUBLIC)

    with pytest.raises(CaptureRejected) as exc:
        validate_capture_url(url, resolver=resolver)
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/docs?q=1",
        "http://8.8.8.8/health",
        "http://[2606:4700:4700::1111]/",
        "http://[::ffff:8.8.8.8]/",
        "HTTP://Example.COM/A",
    ],
)
def test_public_urls_are_allowed(url):
    assert validate_capture_url(url, resolver=_public) == url.strip()


def test_mixed_dns_answers_block_the_url():
    def resolver(_host: str) -> list[str]:
        return ["93.184.216.34", "169.254.169.254"]

    with pytest.raises(CaptureRejected) as exc:
        validate_capture_url("https://example.com/", resolver=resolver)
    assert exc.value.reason == "blocked_url"


def test_public_ip_literal_does_not_resolve():
    def boom(_host: str) -> list[str]:
        raise AssertionError("public IP literals must not be resolved")

    assert validate_capture_url("http://8.8.8.8/", resolver=boom) == "http://8.8.8.8/"


def test_nxdomain_is_terminal(monkeypatch):
    def boom(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr("frontend.capture.socket.getaddrinfo", boom)
    with pytest.raises(CaptureRejected) as exc:
        validate_capture_url("https://missing.example/", resolver=_REAL_RESOLVE)
    assert exc.value.reason == "bad_url"


def test_dns_again_is_retryable(monkeypatch):
    def boom(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    monkeypatch.setattr("frontend.capture.socket.getaddrinfo", boom)
    with pytest.raises(CaptureTransient) as exc:
        validate_capture_url("https://example.com/", resolver=_REAL_RESOLVE)
    assert exc.value.reason == "dns_unavailable"


def test_request_guard_allows_data_and_blocks_metadata():
    ensure_request_allowed("data:image/png;base64,aaaa", resolver=_public)
    with pytest.raises(CaptureRejected) as exc:
        ensure_request_allowed(
            "http://169.254.169.254/computeMetadata/v1/", resolver=_public
        )
    assert exc.value.reason == "blocked_url"


def test_proxy_image_installs_chromium_for_capture():
    dockerfile = Path("frontend/Dockerfile").read_text()
    requirements = Path("frontend/requirements.txt").read_text()
    assert "playwright install" in dockerfile
    assert "chromium" in dockerfile
    assert "playwright>=" in requirements


@pytest.mark.asyncio
async def test_capture_url_202_does_not_render_and_persists_the_job(_capture_env):
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = asyncio.get_running_loop().time()
        response = await client.post(
            "/capture/url?wait=1",
            json={"url": "https://example.com/docs", "subject": "Docs"},
        )
        elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 202
    assert elapsed < 2
    body = response.json()
    assert set(body) == {"status", "job_id", "image_path"}
    assert body["status"] == "accepted"
    assert body["image_path"].startswith("/media/")
    assert body["image_path"].endswith("_page.jpg")
    assert "summary" not in body

    disk = json.loads((_capture_env / "jobs" / f"{body['job_id']}.json").read_text())
    assert disk["status"] == "pending"
    assert disk["source_url"] == "https://example.com/docs"
    assert disk["capture_kind"] == "url"
    assert disk["subject"] == "Docs"
    assert disk["image_path"] == body["image_path"]
    assert disk["capture_rendered"] is False
    assert not (_capture_env / body["image_path"].rsplit("/", 1)[-1]).exists()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/capture/url", json={"subject": "no url"})
        assert missing.status_code == 400


@pytest.mark.asyncio
async def test_capture_url_polls_pending_then_succeeded_with_flair_metadata(
    _capture_env, monkeypatch
):
    renders = {"n": 0}

    def render(url: str) -> bytes:
        renders["n"] += 1
        assert url == "https://example.com/docs"
        return _jpeg_bytes()

    monkeypatch.setattr("frontend.capture.render_page_screenshot", render)
    seen = {}

    async def fake_ingest(*args, capture=None):
        seen["args"] = args
        seen["capture"] = capture
        seen["prompt"] = _upload_ingest_prompt(args[1], args[3], args[4], capture)
        return "Stored the page at https://example.com/docs"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url",
            json={"url": "https://example.com/docs", "subject": "Docs"},
        )
        assert accepted.status_code == 202
        assert renders["n"] == 0
        job_id = accepted.json()["job_id"]
        image_path = accepted.json()["image_path"]
        pending = await client.get(f"/jobs/{job_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"

        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        consumer = await client.post("/ingest", json={"job_id": job_id})
        assert consumer.status_code == 200
        assert consumer.json()["completed"] == [job_id]
        done = await client.get(f"/jobs/{job_id}")
        assert done.json()["status"] == "succeeded"
        assert done.json()["summary"] == "Stored the page at https://example.com/docs"

    assert renders["n"] == 1
    image_name = image_path.rsplit("/", 1)[-1]
    stored = (_capture_env / image_name).read_bytes()
    assert stored.startswith(b"\xff\xd8")
    assert seen["args"][0].startswith(b"\xff\xd8")
    assert seen["args"][4] == "Docs"
    assert seen["capture"]["capture_kind"] == "url"
    assert seen["capture"]["source_url"] == "https://example.com/docs"
    assert seen["capture"]["image_url"] == image_path
    assert seen["capture"]["captured_at"].endswith("Z")
    assert "capture_kind" in seen["prompt"]
    assert "https://example.com/docs" in seen["prompt"]
    assert json.dumps(seen["capture"]) in seen["prompt"]
    record = load_job(job_id)
    assert record["capture_rendered"] is True
    assert record["captured_at"] == seen["capture"]["captured_at"]
    assert record["source_url"] == "https://example.com/docs"


@pytest.mark.asyncio
async def test_capture_terminal_urls_fail_the_job_and_do_not_retry(
    _capture_env, monkeypatch
):
    renders = {"n": 0}

    def render(_url: str) -> bytes:
        renders["n"] += 1
        return _jpeg_bytes()

    monkeypatch.setattr("frontend.capture.render_page_screenshot", render)

    async def ingest_must_not_run(*_args, **_kwargs):
        raise AssertionError("extract must not run for a rejected URL")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", ingest_must_not_run)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for url, reason in (
            ("ftp://example.com/secret", "unsupported_scheme"),
            ("http://169.254.169.254/computeMetadata/v1/", "blocked_url"),
            ("http://10.0.0.5/", "blocked_url"),
            ("not-a-url", "bad_url"),
        ):
            accepted = await client.post("/capture/url", json={"url": url})
            assert accepted.status_code == 202
            job_id = accepted.json()["job_id"]
            pending = await client.get(f"/jobs/{job_id}")
            assert pending.json()["status"] == "pending"
            consumer = await client.post("/ingest", json={"job_id": job_id})
            assert consumer.status_code == 200
            failed = await client.get(f"/jobs/{job_id}")
            assert failed.json()["status"] == "failed"
            assert failed.json()["error"] == reason
            again = await client.post("/ingest", json={"job_id": job_id})
            assert again.status_code == 200
            assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "failed"
    assert renders["n"] == 0


@pytest.mark.asyncio
async def test_render_timeout_is_terminal(monkeypatch):
    def render(_url: str) -> bytes:
        raise CaptureRejected("render_timeout")

    monkeypatch.setattr("frontend.capture.render_page_screenshot", render)

    async def ingest_must_not_run(*_args, **_kwargs):
        raise AssertionError("extract must not run after render_timeout")

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", ingest_must_not_run)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url", json={"url": "https://example.com/slow"}
        )
        job_id = accepted.json()["job_id"]
        await client.post("/ingest", json={"job_id": job_id})
        failed = await client.get(f"/jobs/{job_id}")
        assert failed.json()["status"] == "failed"
        assert failed.json()["error"] == "render_timeout"
        await client.post("/ingest", json={"job_id": job_id})
        assert (await client.get(f"/jobs/{job_id}")).json()["error"] == "render_timeout"


@pytest.mark.asyncio
async def test_transient_render_stays_pending_then_succeeds(monkeypatch):
    state = {"n": 0}

    def render(url: str) -> bytes:
        state["n"] += 1
        if state["n"] == 1:
            raise CaptureTransient("render_failed")
        assert url == "https://example.com/flaky"
        return _jpeg_bytes()

    monkeypatch.setattr("frontend.capture.render_page_screenshot", render)
    ingest_calls = {"n": 0}

    async def fake_ingest(*_args, **_kwargs):
        ingest_calls["n"] += 1
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url", json={"url": "https://example.com/flaky"}
        )
        job_id = accepted.json()["job_id"]
        first = await client.post("/ingest", json={"job_id": job_id})
        assert first.status_code == 503
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "pending"
        assert ingest_calls["n"] == 0
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        second = await client.post("/ingest", json={"job_id": job_id})
        assert second.status_code == 200
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"
    assert state["n"] == 2
    assert ingest_calls["n"] == 1


@pytest.mark.asyncio
async def test_extract_retry_does_not_render_again(_capture_env, monkeypatch):
    renders = {"n": 0}

    def render(_url: str) -> bytes:
        renders["n"] += 1
        return _jpeg_bytes()

    monkeypatch.setattr("frontend.capture.render_page_screenshot", render)
    ingest_calls = {"n": 0}

    async def flaky_ingest(*_args, **_kwargs):
        ingest_calls["n"] += 1
        if ingest_calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", flaky_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url", json={"url": "https://example.com/once"}
        )
        job_id = accepted.json()["job_id"]
        first = await client.post("/ingest", json={"job_id": job_id})
        assert first.status_code == 503
        assert renders["n"] == 1
        image_name = accepted.json()["image_path"].rsplit("/", 1)[-1]
        assert (_capture_env / image_name).is_file()
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        second = await client.post("/ingest", json={"job_id": job_id})
        assert second.status_code == 200
    assert renders["n"] == 1
    assert ingest_calls["n"] == 2
    assert load_job(job_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_capture_enqueues_cloud_task_with_durable_url(monkeypatch):
    created = []
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr(
        "frontend.main.create_ingest_cloud_task",
        lambda job_id: created.append(job_id),
    )

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url",
            json={"url": "https://example.com/docs", "subject": "Docs"},
        )
    assert accepted.status_code == 202
    assert created == [accepted.json()["job_id"]]


@pytest.mark.asyncio
async def test_capture_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setattr("frontend.main.API_KEY", "secret-key")
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", False)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/capture/url", json={"url": "https://example.com/"})
        assert denied.status_code == 401
        allowed = await client.post(
            "/capture/url",
            json={"url": "https://example.com/"},
            headers={"X-Api-Key": "secret-key"},
        )
        assert allowed.status_code == 202


@pytest.mark.asyncio
async def test_existing_screenshot_is_not_rendered_again(_capture_env, monkeypatch):
    async def fake_ingest(*_args, **_kwargs):
        return "stored"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/url", json={"url": "https://example.com/cached"}
        )
        image_name = accepted.json()["image_path"].rsplit("/", 1)[-1]
        persist_uploaded_image(_jpeg_bytes(), image_name, "image/jpeg")
        consumer = await client.post(
            "/ingest", json={"job_id": accepted.json()["job_id"]}
        )
        assert consumer.status_code == 200
    assert load_job(accepted.json()["job_id"])["capture_rendered"] is True
