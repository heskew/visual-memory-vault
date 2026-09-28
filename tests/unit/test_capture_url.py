"""URL capture: scheme/address policy, 202 contract, and worker outcomes."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import socket
import sys
import types
import zlib
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from frontend import capture as capture_mod
from frontend.capture import (
    CHROMIUM_LAUNCH_ARGS,
    WEBRTC_DISABLE_SCRIPT,
    CaptureRejected,
    CaptureTransient,
    capture_store_metadata,
    ensure_request_allowed,
    fetch_pinned_response,
    handle_capture_route,
    pin_public_address,
    pin_public_addresses,
    validate_capture_url,
)
from frontend.main import (
    _upload_ingest_prompt,
    load_job,
    persist_uploaded_image,
)

_REAL_RESOLVE = capture_mod.resolve_host
_REAL_RENDER = capture_mod.render_page_screenshot

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


class _Frame:
    def __init__(self, parent: _Frame | None) -> None:
        self.parent_frame = parent


class _Request:
    def __init__(
        self,
        url: str,
        *,
        resource_type: str = "document",
        frame: _Frame | None = None,
        navigation: bool = True,
    ) -> None:
        self.url = url
        self.resource_type = resource_type
        self.frame = frame
        self.method = "GET"
        self.headers: dict[str, str] = {}
        self.post_data_buffer = None
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.action = None
        self.fulfilled = None

    def abort(self, *_args, **_kwargs) -> None:
        self.action = "abort"

    def continue_(self, **_kwargs) -> None:
        self.action = "continue"

    def fulfill(self, **kwargs) -> None:
        self.action = "fulfill"
        self.fulfilled = kwargs


def _must_not_fetch(*_args, **_kwargs):
    raise AssertionError("this request must not be fetched")


def test_blocked_iframe_does_not_fail_capture():
    """A bad iframe is aborted. The top-level page is still fetched."""
    blocked = {"document": False}
    main = _Frame(None)
    iframe = _Frame(main)

    def resolver(host: str) -> list[str]:
        if host == "missing.example":
            raise CaptureRejected("bad_url")
        return ["93.184.216.34"]

    for url in (
        "http://10.1.2.3/frame",
        "http://169.254.169.254/computeMetadata/v1/",
        "file:///etc/passwd",
        "https://missing.example/embed",
    ):
        route = _Route(
            _Request(url, resource_type="document", frame=iframe, navigation=True)
        )
        handle_capture_route(route, blocked, resolver=resolver, fetch=_must_not_fetch)
        assert route.action == "abort", url
        assert blocked["document"] is False, url

    image = _Route(
        _Request(
            "http://192.168.0.9/pixel.gif",
            resource_type="image",
            frame=main,
            navigation=False,
        )
    )
    handle_capture_route(image, blocked, resolver=resolver, fetch=_must_not_fetch)
    assert image.action == "abort"
    assert blocked["document"] is False

    def fetch(*_args, **_kwargs):
        return 200, {"Content-Type": "text/html"}, b"<html>ok</html>"

    page = _Route(
        _Request(
            "https://example.com/docs",
            resource_type="document",
            frame=main,
            navigation=True,
        )
    )
    handle_capture_route(page, blocked, resolver=resolver, fetch=fetch)
    assert page.action == "fulfill"
    assert page.fulfilled["body"] == b"<html>ok</html>"
    assert blocked["document"] is False


def test_blocked_main_frame_still_fails_capture():
    blocked = {"document": False}
    route = _Route(
        _Request(
            "http://169.254.169.254/",
            resource_type="document",
            frame=_Frame(None),
            navigation=True,
        )
    )
    handle_capture_route(route, blocked, resolver=_public, fetch=_must_not_fetch)
    assert route.action == "abort"
    assert blocked["document"] is True


def test_pin_does_not_reuse_an_earlier_public_answer():
    answers = [["8.8.8.8"], ["1.1.1.1"], ["169.254.169.254"]]

    def resolver(_host: str) -> list[str]:
        return answers.pop(0)

    assert pin_public_address("example.com", resolver=resolver) == "8.8.8.8"
    assert pin_public_address("example.com", resolver=resolver) == "1.1.1.1"
    with pytest.raises(CaptureRejected) as exc:
        pin_public_address("example.com", resolver=resolver)
    assert exc.value.reason == "blocked_url"
    assert answers == []


def test_pin_rejects_a_mixed_public_and_private_answer():
    def resolver(_host: str) -> list[str]:
        return ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(CaptureRejected) as exc:
        pin_public_address("example.com", resolver=resolver)
    assert exc.value.reason == "blocked_url"


def test_pinned_fetch_dials_the_validated_ip_not_the_hostname(monkeypatch):
    seen = {}

    def connect(address, timeout=None, source_address=None):
        seen["address"] = address
        raise TimeoutError("stop before tls")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)
    with pytest.raises(CaptureTransient):
        fetch_pinned_response(
            "GET",
            "https://example.com/docs",
            {},
            None,
            resolver=lambda _host: ["93.184.216.34"],
        )
    assert seen["address"] == ("93.184.216.34", 443)


def test_connect_time_rebinding_does_not_open_a_socket(monkeypatch):
    calls = {"n": 0}
    connected = {"yes": False}

    def resolver(_host: str) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return ["93.184.216.34"]
        return ["169.254.169.254"]

    def connect(*_args, **_kwargs):
        connected["yes"] = True
        raise AssertionError("rebinding must not open a socket")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)
    blocked = {"document": False}
    route = _Route(
        _Request(
            "https://rebind.example/secret",
            resource_type="document",
            frame=_Frame(None),
            navigation=True,
        )
    )
    handle_capture_route(route, blocked, resolver=resolver)
    assert route.action == "abort"
    assert blocked["document"] is True
    assert connected["yes"] is False
    assert calls["n"] >= 2


def test_chromium_cannot_resolve_on_its_own():
    assert "--disable-quic" in CHROMIUM_LAUNCH_ARGS
    assert "--dns-prefetch-disable" in CHROMIUM_LAUNCH_ARGS
    assert (
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
        in CHROMIUM_LAUNCH_ARGS
    )
    assert not any(
        arg == "--disable-features=WebRTC" or arg.endswith("=WebRTC")
        for arg in CHROMIUM_LAUNCH_ARGS
    )
    for name in ("RTCPeerConnection", "webkitRTCPeerConnection"):
        assert name in WEBRTC_DISABLE_SCRIPT


def test_render_disables_webrtc_before_page_scripts(monkeypatch):
    launched: dict = {}

    class _Page:
        def set_default_navigation_timeout(self, _ms):
            return None

        def set_default_timeout(self, _ms):
            return None

        def route(self, _pattern, _handler):
            return None

        def goto(self, _url, wait_until=None, timeout=None):
            return None

        def screenshot(self, type=None, quality=None, full_page=None):
            return b"jpeg-bytes"

    class _Context:
        def __init__(self):
            self.scripts: list[str] = []

        def add_init_script(self, script):
            self.scripts.append(script)

        def new_page(self):
            return _Page()

    class _Browser:
        def new_context(self, **_kwargs):
            context = _Context()
            launched["context"] = context
            return context

    class _Chromium:
        def launch(self, headless=None, args=None):
            launched["args"] = list(args or [])
            return _Browser()

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.TimeoutError = type("PlaywrightTimeout", (Exception,), {})
    sync_api.sync_playwright = lambda: _Playwright()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    assert _REAL_RENDER("https://example.com/") == b"jpeg-bytes"
    assert "--disable-quic" in launched["args"]
    assert (
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in launched["args"]
    )
    assert "--disable-features=WebRTC" not in launched["args"]
    assert WEBRTC_DISABLE_SCRIPT in launched["context"].scripts


_V6 = "2606:4700:4700::1111"
_V4 = "93.184.216.34"


def test_aaaa_first_prefers_ipv4_when_ipv6_egress_is_down():
    def resolver(_host: str) -> list[str]:
        return [_V6, _V4]

    assert pin_public_addresses("example.com", resolver=resolver, ipv6_ok=False) == [
        _V4,
        _V6,
    ]
    assert pin_public_address("example.com", resolver=resolver, ipv6_ok=False) == _V4


def test_aaaa_first_keeps_dns_order_when_ipv6_egress_works():
    def resolver(_host: str) -> list[str]:
        return [_V6, _V4]

    assert pin_public_addresses("example.com", resolver=resolver, ipv6_ok=True) == [
        _V6,
        _V4,
    ]


def test_ipv6_only_public_name_is_not_replaced():
    def resolver(_host: str) -> list[str]:
        return [_V6]

    assert pin_public_address("example.com", resolver=resolver, ipv6_ok=False) == _V6


def test_pin_rejects_aaaa_mixed_with_a_private_answer(monkeypatch):
    def connect(*_args, **_kwargs):
        raise AssertionError("mixed answer must not dial")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)

    def resolver(_host: str) -> list[str]:
        return [_V6, "10.0.0.1"]

    with pytest.raises(CaptureRejected) as exc:
        pin_public_address("example.com", resolver=resolver, ipv6_ok=False)
    assert exc.value.reason == "blocked_url"


def test_fetch_does_not_dial_when_any_answer_is_private(monkeypatch):
    connected = {"yes": False}

    def connect(*_args, **_kwargs):
        connected["yes"] = True
        raise AssertionError("private answer must not dial")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)
    with pytest.raises(CaptureRejected) as exc:
        fetch_pinned_response(
            "GET",
            "https://example.com/",
            None,
            None,
            resolver=lambda _host: [_V6, "169.254.169.254"],
            ipv6_ok=True,
        )
    assert exc.value.reason == "blocked_url"
    assert connected["yes"] is False


def test_pinned_fetch_falls_back_from_unreachable_ipv6(monkeypatch):
    seen: list[tuple] = []

    def connect(address, timeout=None, source_address=None):
        seen.append(address)
        if address[0] == _V6:
            raise OSError(101, "Network is unreachable")
        raise TimeoutError("stop after ipv4")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)
    with pytest.raises(CaptureTransient):
        fetch_pinned_response(
            "GET",
            "https://example.com/docs",
            {"Accept-Encoding": "gzip, deflate, br"},
            None,
            resolver=lambda _host: [_V6, _V4],
            ipv6_ok=True,
        )
    assert seen == [(_V6, 443), (_V4, 443)]


def test_pinned_fetch_dials_ipv4_first_without_ipv6_egress(monkeypatch):
    seen: list[tuple] = []

    def connect(address, timeout=None, source_address=None):
        seen.append(address)
        raise TimeoutError("stop")

    monkeypatch.setattr("frontend.capture.socket.create_connection", connect)
    with pytest.raises(CaptureTransient):
        fetch_pinned_response(
            "GET",
            "https://example.com/docs",
            None,
            None,
            resolver=lambda _host: [_V6, _V4],
            ipv6_ok=False,
        )
    assert seen[0] == (_V4, 443)
    assert (_V6, 443) in seen


class _PinnedResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body
        self._pos = 0

    def getheaders(self):
        return list(self._headers)

    def read(self, size):
        if self._pos >= len(self._body):
            return b""
        chunk = self._body[self._pos : self._pos + size]
        self._pos += size
        return chunk


class _PinnedConn:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent_headers = None

    def request(self, method, path, body=None, headers=None):
        self.sent_headers = dict(headers or {})

    def close(self):
        return None


def _patch_pinned_conn(monkeypatch, response: _PinnedResponse):
    sent = {}

    class Conn(_PinnedConn):
        def request(self, method, path, body=None, headers=None):
            super().request(method, path, body=body, headers=headers)
            sent["headers"] = self.sent_headers

        def getresponse(self):
            return response

    monkeypatch.setattr("frontend.capture.http.client.HTTPSConnection", Conn)
    return sent


def test_gzip_body_is_decoded_before_fulfill(monkeypatch):
    plain = b"<html>ok</html>"
    sent = _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [
                ("Content-Encoding", "gzip"),
                ("Content-Type", "text/html"),
                ("Content-Length", "99"),
            ],
            gzip.compress(plain),
        ),
    )
    status, headers, body = fetch_pinned_response(
        "GET",
        "https://example.com/docs",
        {"Accept-Encoding": "gzip, deflate, br, zstd", "User-Agent": "Mozilla"},
        None,
        resolver=lambda _host: [_V4],
        ipv6_ok=True,
    )
    assert status == 200
    assert body == plain
    assert all(key.lower() != "content-encoding" for key in headers)
    assert all(key.lower() != "content-length" for key in headers)
    assert headers["Content-Type"] == "text/html"
    assert sent["headers"]["Accept-Encoding"] == "identity"
    assert "gzip" not in sent["headers"]["Accept-Encoding"]


def test_deflate_body_is_decoded_before_fulfill(monkeypatch):
    plain = b"<html>deflate</html>"
    _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [("Content-Encoding", "deflate"), ("Content-Type", "text/html")],
            zlib.compress(plain),
        ),
    )
    _status, headers, body = fetch_pinned_response(
        "GET",
        "https://example.com/",
        None,
        None,
        resolver=lambda _host: [_V4],
        ipv6_ok=True,
    )
    assert body == plain
    assert all(key.lower() != "content-encoding" for key in headers)


def test_raw_deflate_body_is_decoded_before_fulfill(monkeypatch):
    plain = b"<html>raw</html>"
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(plain) + compressor.flush()
    _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [("Content-Encoding", "deflate")],
            raw,
        ),
    )
    _status, _headers, body = fetch_pinned_response(
        "GET",
        "https://example.com/",
        None,
        None,
        resolver=lambda _host: [_V4],
        ipv6_ok=True,
    )
    assert body == plain


def test_unsupported_content_encoding_is_not_fulfilled(monkeypatch):
    _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [("Content-Encoding", "br"), ("Content-Type", "text/html")],
            b"\x8b not brotli",
        ),
    )
    with pytest.raises(CaptureTransient) as exc:
        fetch_pinned_response(
            "GET",
            "https://example.com/",
            {"Accept-Encoding": "br"},
            None,
            resolver=lambda _host: [_V4],
            ipv6_ok=True,
        )
    assert exc.value.reason == "render_failed"


def _compressed_zeros(count: int, *, wbits: int) -> bytes:
    """Compress ``count`` zero bytes without keeping that buffer around."""
    compressor = zlib.compressobj(wbits=wbits)
    parts: list[bytes] = []
    remaining = count
    chunk = b"\0" * 65536
    while remaining:
        take = min(remaining, len(chunk))
        parts.append(compressor.compress(chunk[:take]))
        remaining -= take
    parts.append(compressor.flush())
    return b"".join(parts)


def _track_capped_inflate(monkeypatch):
    """Fail the test if inflate asks zlib for an unlimited output buffer."""
    produced: list[int] = []
    real = zlib.decompressobj
    cap = capture_mod.CAPTURE_MAX_SUBRESOURCE_BYTES + 1

    class _Tracking:
        def __init__(self, wbits=zlib.MAX_WBITS):
            self._inner = real(wbits)

        def decompress(self, data, max_length=0):
            if not max_length or max_length > cap:
                raise AssertionError(f"inflate asked for {max_length} bytes")
            out = self._inner.decompress(data, max_length)
            assert len(out) <= cap
            produced.append(len(out))
            return out

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(capture_mod.zlib, "decompressobj", _Tracking)
    return produced


def _assert_expanding_body_is_rejected(monkeypatch, *, encoding: str, wbits: int):
    limit = capture_mod.CAPTURE_MAX_SUBRESOURCE_BYTES
    logical = limit + (1 << 20)
    payload = _compressed_zeros(logical, wbits=wbits)
    assert len(payload) < limit
    produced = _track_capped_inflate(monkeypatch)
    _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [("Content-Encoding", encoding), ("Content-Type", "text/html")],
            payload,
        ),
    )
    with pytest.raises(CaptureTransient) as exc:
        fetch_pinned_response(
            "GET",
            "https://example.com/",
            {"Accept-Encoding": "gzip, deflate, br"},
            None,
            resolver=lambda _host: [_V4],
            ipv6_ok=True,
        )
    assert exc.value.reason == "render_failed"
    assert produced
    assert sum(produced) < logical
    assert max(produced) <= limit + 1


def test_gzip_bomb_is_rejected_without_full_inflate(monkeypatch):
    _assert_expanding_body_is_rejected(
        monkeypatch, encoding="gzip", wbits=16 + zlib.MAX_WBITS
    )


def test_deflate_bomb_is_rejected_without_full_inflate(monkeypatch):
    _assert_expanding_body_is_rejected(
        monkeypatch, encoding="deflate", wbits=zlib.MAX_WBITS
    )


def test_raw_deflate_bomb_is_rejected_without_full_inflate(monkeypatch):
    _assert_expanding_body_is_rejected(
        monkeypatch, encoding="deflate", wbits=-zlib.MAX_WBITS
    )


def test_inflate_allows_output_exactly_at_the_cap(monkeypatch):
    monkeypatch.setattr(capture_mod, "CAPTURE_MAX_SUBRESOURCE_BYTES", 128)
    plain = b"A" * 128
    _patch_pinned_conn(
        monkeypatch,
        _PinnedResponse(
            200,
            [("Content-Encoding", "gzip"), ("Content-Type", "text/plain")],
            gzip.compress(plain),
        ),
    )
    _status, headers, body = fetch_pinned_response(
        "GET",
        "https://example.com/",
        None,
        None,
        resolver=lambda _host: [_V4],
        ipv6_ok=True,
    )
    assert body == plain
    assert all(key.lower() != "content-encoding" for key in headers)
