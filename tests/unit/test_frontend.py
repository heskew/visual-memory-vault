import pytest
from fastapi import HTTPException
from starlette.requests import Request

from frontend.main import _extract_parts, verify_api_key


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
