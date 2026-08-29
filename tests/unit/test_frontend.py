import asyncio
import io
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.requests import Request

from frontend.main import (
    _extract_parts,
    extract_receipt_fields,
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

        saved = list(tmp_path.iterdir())
        assert len(saved) == 1
        assert saved[0].name.endswith("receipt.jpg")
        assert saved[0].stat().st_size > 0

        await asyncio.wait_for(ingest_started.wait(), timeout=2)
        assert not ingest_release.is_set()
        ingest_release.set()


@pytest.mark.asyncio
async def test_upload_persists_when_agent_never_starts(tmp_path, monkeypatch):
    """A scheduler that never runs still leaves the photo on disk."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
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
    assert list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_upload_wait_returns_agent_summary(tmp_path, monkeypatch):
    """In-app web UI keeps a synchronous summary via ?wait=1."""
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)

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
