from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from frontend.main import (
    _extract_parts,
    extract_receipt_fields,
    strip_receipt_marker,
    verify_api_key,
)


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
