import base64
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from app import flair_client


@pytest.fixture
def test_ed25519_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.private_bytes_raw()
    return raw, base64.b64encode(raw).decode("utf-8")


def test_get_flair_agent_id(monkeypatch):
    monkeypatch.delenv("FLAIR_AGENT_ID", raising=False)
    assert flair_client.get_flair_agent_id() == "visual-memory-vault"

    monkeypatch.setenv("FLAIR_AGENT_ID", "custom-agent")
    assert flair_client.get_flair_agent_id() == "custom-agent"


def test_get_flair_target(monkeypatch):
    monkeypatch.delenv("FLAIR_TARGET", raising=False)
    monkeypatch.delenv("FLAIR_URL", raising=False)
    assert flair_client.get_flair_target() == "http://127.0.0.1:19926"

    monkeypatch.setenv("FLAIR_TARGET", "https://flair.example.com")
    assert flair_client.get_flair_target() == "https://flair.example.com"


def test_get_key_bytes_b64(monkeypatch, test_ed25519_key):
    raw_key, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)
    monkeypatch.setenv("FLAIR_AGENT_ID", "test-agent")

    key_info = flair_client._get_key_bytes("test-agent")
    assert key_info is not None
    assert key_info[0] == "test-agent"
    assert key_info[1] == raw_key


def test_get_key_bytes_keyfile(monkeypatch, test_ed25519_key):
    raw_key, _ = test_ed25519_key
    with tempfile.NamedTemporaryFile("wb", delete=False) as f:
        f.write(raw_key)
        tmp_path = f.name

    try:
        monkeypatch.delenv("FLAIR_PRIVATE_KEY_B64", raising=False)
        monkeypatch.setenv("FLAIR_KEYFILE", tmp_path)
        monkeypatch.setenv("FLAIR_AGENT_ID", "file-agent")

        key_info = flair_client._get_key_bytes()
        assert key_info is not None
        assert key_info[0] == "file-agent"
        assert key_info[1] == raw_key
    finally:
        os.remove(tmp_path)


def test_sign_header_success(monkeypatch, test_ed25519_key):
    raw_key, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)
    monkeypatch.setenv("FLAIR_AGENT_ID", "vault-bot")

    res = flair_client._sign_header("PUT", "/Memory/123", "vault-bot")
    assert res is not None
    agent_id, auth_hdr = res
    assert agent_id == "vault-bot"
    assert auth_hdr.startswith("TPS-Ed25519 vault-bot:")


def test_sign_header_missing_key(monkeypatch):
    monkeypatch.delenv("FLAIR_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("FLAIR_KEYFILE", raising=False)

    with patch("os.path.exists", return_value=False):
        res = flair_client._sign_header("GET", "/Memory", "non-existent")
        assert res is None


def test_store_memory_rest_success(monkeypatch, test_ed25519_key):
    _, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)
    monkeypatch.setenv("FLAIR_AGENT_ID", "vault-bot")
    monkeypatch.setenv("FLAIR_TARGET", "http://localhost:19926")

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.text = '{"id":"123","written":true}'

    with patch("httpx.Client.put", return_value=mock_resp):
        res = flair_client.store_memory(
            subject="Receipt",
            content="Coffee $5.00",
            tags=["cafe"],
            durability="persistent",
        )
        assert res["status"] == "success"
        assert res["subject"] == "Receipt"
        assert "123" in res["output"]


def test_store_memory_rest_error(monkeypatch, test_ed25519_key):
    _, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)
    monkeypatch.setenv("FLAIR_AGENT_ID", "vault-bot")

    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("httpx.Client.put", return_value=mock_resp):
        with patch("shutil.which", return_value=None):
            res = flair_client.store_memory(subject="Test", content="Text")
            assert res["status"] == "error"
            assert "HTTP 500" in res["message"]


def test_store_memory_cli_fallback(monkeypatch):
    monkeypatch.delenv("FLAIR_PRIVATE_KEY_B64", raising=False)
    monkeypatch.delenv("FLAIR_KEYFILE", raising=False)

    with patch("os.path.exists", return_value=False):
        with patch("shutil.which", return_value="/usr/local/bin/flair"):
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = '{"id":"cli-123","written":true}'
            with patch("subprocess.run", return_value=mock_proc):
                res = flair_client.store_memory(
                    subject="CLI Receipt", content="Book $20", tags=["books"]
                )
                assert res["status"] == "success"
                assert res["subject"] == "CLI Receipt"


def test_search_memories_rest_success(monkeypatch, test_ed25519_key):
    _, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.text = '{"results":[{"content":"Coffee $5"}]}'

    with patch("httpx.Client.post", return_value=mock_resp):
        res = flair_client.search_memories(query="Coffee", limit=3)
        assert res["status"] == "success"
        assert "Coffee $5" in res["results"]


def test_list_memories_rest_success(monkeypatch, test_ed25519_key):
    _, b64_key = test_ed25519_key
    monkeypatch.setenv("FLAIR_PRIVATE_KEY_B64", b64_key)

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.text = '[{"id":"1","subject":"WiFi"}]'

    with patch("httpx.Client.get", return_value=mock_resp):
        res = flair_client.list_memories()
        assert res["status"] == "success"
        assert "WiFi" in res["memories"]
