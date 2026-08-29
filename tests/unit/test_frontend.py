import asyncio
import io
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.requests import Request

from frontend.main import (
    _extract_parts,
    drain_pending_ingest_jobs,
    extract_receipt_fields,
    persist_ingest_job,
    persist_uploaded_image,
    strip_receipt_marker,
    verify_api_key,
)


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(200, 10, 10)).save(buf, format="JPEG")
    return buf.getvalue()


def test_verify_api_key_when_no_key_configured(monkeypatch):
    monkeypatch.setattr("frontend.main.API_KEY", "")
    scope = {"type": "http", "headers": [], "query_string": b""}
    req = Request(scope)
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
    assert "/upload?wait=1" in html


@pytest.mark.asyncio
async def test_upload_returns_before_hung_agent(tmp_path, monkeypatch):
    """Shortcut POST /upload must ack before A2A/Gemini finishes."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    ingest_started = asyncio.Event()
    ingest_release = asyncio.Event()

    async def hung_ingest(*args, **kwargs):
        ingest_started.set()
        await ingest_release.wait()
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
        assert body["filename"] == "receipt.jpg"
        assert body["image_path"].startswith("/media/")
        assert "summary" not in body
        assert "reply" not in body
        assert "merchant" not in body

        images = [p for p in tmp_path.iterdir() if not p.name.endswith(".ingest.json")]
        jobs = list(tmp_path.glob("*.ingest.json"))
        assert len(images) == 1
        assert images[0].name.endswith("receipt.jpg")
        assert images[0].stat().st_size > 0
        assert len(jobs) == 1

        await asyncio.wait_for(ingest_started.wait(), timeout=2)
        assert not ingest_release.is_set()
        ingest_release.set()


@pytest.mark.asyncio
async def test_upload_persists_when_agent_never_starts(tmp_path, monkeypatch):
    """A scheduler that never runs still leaves the photo and ingest job on disk."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.schedule_memory_ingest", lambda *a, **k: None)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post(
                "/upload",
                files={"file": ("wifi.jpg", _jpeg_bytes(), "image/jpeg")},
            ),
            timeout=3,
        )

    assert response.status_code == 202
    images = [p for p in tmp_path.iterdir() if not p.name.endswith(".ingest.json")]
    jobs = list(tmp_path.glob("*.ingest.json"))
    assert len(images) == 1
    assert images[0].name.endswith("wifi.jpg")
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text())
    assert job["filename"] == "wifi.jpg"
    assert job["protected_url"].startswith("/media/")
    assert job["image_name"].endswith("wifi.jpg")


@pytest.mark.asyncio
async def test_upload_wait_returns_agent_summary(tmp_path, monkeypatch):
    """In-app web UI keeps a synchronous summary via ?wait=1."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    async def fake_ingest(*args, **kwargs):
        return (
            "Saved dinner at Joe's Grill.\n"
            'RECEIPT: {"merchant":"Joe\'s Grill","amount":"58.40","currency":"USD","date":"2026-08-20"}'
        )

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)

    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/upload?wait=1",
            files={"file": ("receipt.jpg", _jpeg_bytes(), "image/jpeg")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["reply"] == "Saved dinner at Joe's Grill."
    assert body["summary"] == "Saved dinner at Joe's Grill."
    assert body["merchant"] == "Joe's Grill"
    assert body["amount"] == "58.40"
    assert body["currency"] == "USD"
    assert body["date"] == "2026-08-20"
    assert list(tmp_path.iterdir())
    assert not list(tmp_path.glob("*.ingest.json"))


@pytest.mark.asyncio
async def test_drain_pending_ingest_jobs_calls_ingest(tmp_path, monkeypatch):
    """Drain loads a persisted job and calls ingest_uploaded_image (no live Flair)."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    image_name = "abc_shot.jpg"
    persist_uploaded_image(_jpeg_bytes(), image_name, "image/jpeg")
    persist_ingest_job(
        image_name, "shot.jpg", "image/jpeg", f"/media/{image_name}", "WiFi"
    )
    assert list(tmp_path.glob("*.ingest.json"))

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

    assert completed == [image_name]
    assert len(calls) == 1
    assert calls[0]["filename"] == "shot.jpg"
    assert calls[0]["subject"] == "WiFi"
    assert calls[0]["protected_url"] == f"/media/{image_name}"
    assert calls[0]["nbytes"] > 0
    assert not list(tmp_path.glob("*.ingest.json"))
    assert (tmp_path / image_name).exists()


@pytest.mark.asyncio
async def test_ingest_endpoint_runs_one_persisted_job(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)

    image_name = "xyz_menu.jpg"
    persist_uploaded_image(_jpeg_bytes(), image_name, "image/jpeg")
    persist_ingest_job(
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
    assert response.json() == {"status": "ok", "completed": [image_name]}
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.ingest.json"))
