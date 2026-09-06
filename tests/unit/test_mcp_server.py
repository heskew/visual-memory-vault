import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from adk_flair import FlairMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.app_utils import services
from mcp_server import server


class FakeFlair(FlairMemoryService):
    def __init__(self):
        self.search_memory = AsyncMock(return_value=SearchMemoryResponse(memories=[]))
        self.list_memories = AsyncMock(return_value=[])
        self.add_memory = AsyncMock(return_value=None)


def _entry(mem_id, text, **meta):
    return MemoryEntry(
        id=mem_id,
        content=types.Content(role="user", parts=[types.Part(text=text)]),
        timestamp="2026-08-22T00:00:00Z",
        custom_metadata=meta,
    )


@pytest.mark.asyncio
async def test_search_formats_and_enriches(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    fake = FakeFlair()
    fake.search_memory = AsyncMock(
        return_value=SearchMemoryResponse(
            memories=[
                _entry(
                    "m1",
                    "Total $58.40",
                    subject="Joe's Grill",
                    image_url="/media/x.jpg",
                    merchant="Joe's Grill",
                    amount="58.40",
                )
            ]
        )
    )
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._search_impl("joes", limit=5)
    assert res["count"] == 1
    m = res["memories"][0]
    assert m["subject"] == "Joe's Grill"
    assert m["merchant"] == "Joe's Grill"
    assert "Total $58.40" in m["content"]
    assert m["image_url"] == "/media/x.jpg"  # no bucket/proxy -> raw reference


def test_viewable_image_url_proxy_fallback(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    monkeypatch.setenv("VAULT_PROXY_URL", "https://proxy.example")
    assert (
        server.viewable_image_url("/media/x.jpg") == "https://proxy.example/media/x.jpg"
    )


def test_viewable_image_url_none(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    assert server.viewable_image_url(None) is None


@pytest.mark.asyncio
async def test_store_sets_stable_id(monkeypatch):
    fake = FakeFlair()
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._store_impl(
            "Receipt", "Total $58.40", image_url="/media/x.jpg"
        )
    assert res["status"] == "stored"
    entry = fake.add_memory.await_args.kwargs["memories"][0]
    assert entry.id == hashlib.sha256(b"vault:/media/x.jpg").hexdigest()[:32]


@pytest.mark.asyncio
async def test_list_formats(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    fake = FakeFlair()
    fake.list_memories = AsyncMock(return_value=[_entry("m1", "hi", subject="S")])
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._list_impl()
    assert res["count"] == 1
    assert res["memories"][0]["subject"] == "S"


@pytest.mark.asyncio
async def test_tools_are_registered():
    tools = await server.server.list_tools()
    names = {t.name for t in tools}
    assert {"search_vault", "list_vault", "store_vault"} <= names


def test_format_reads_first_class_subject_not_blob():
    """Flair stores subject on the record column, not the metadata blob.

    MemoryEntry has no subject field; wrappers may attach entry.subject, and
    FlairMemoryService remaps the column onto custom_metadata['subject'] on
    read. A blob-only reader returns null for real vault rows.
    """
    entry = MemoryEntry(
        id="m1",
        content=types.Content(role="user", parts=[types.Part(text="wifi")]),
        timestamp="2026-08-22T00:00:00Z",
        custom_metadata={"image_url": "/media/x.jpg"},
    )
    entry_with_column = SimpleNamespace(
        id=entry.id,
        content=entry.content,
        timestamp=entry.timestamp,
        custom_metadata=entry.custom_metadata,
        subject="Hotel WiFi",
    )
    assert server._format(entry_with_column)["subject"] == "Hotel WiFi"


def test_format_reads_flair_remapped_column_in_custom_metadata():
    """FlairMemoryService._hit_custom_metadata copies the column into the blob."""
    entry = _entry("m1", "wifi", subject="Hotel WiFi", image_url="/media/x.jpg")
    assert server._format(entry)["subject"] == "Hotel WiFi"


@pytest.mark.asyncio
async def test_search_returns_first_class_subject(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    fake = FakeFlair()
    hit = SimpleNamespace(
        id="m1",
        content=types.Content(role="user", parts=[types.Part(text="Total $58.40")]),
        timestamp="2026-08-22T00:00:00Z",
        custom_metadata={"image_url": "/media/x.jpg", "merchant": "Joe's Grill"},
        subject="Joe's Grill",
    )
    fake.search_memory = AsyncMock(return_value=SimpleNamespace(memories=[hit]))
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._search_impl("joes")
    assert res["memories"][0]["subject"] == "Joe's Grill"


@pytest.mark.asyncio
async def test_list_returns_first_class_subject(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    fake = FakeFlair()
    hit = SimpleNamespace(
        id="m1",
        content=types.Content(role="user", parts=[types.Part(text="hi")]),
        timestamp="2026-08-22T00:00:00Z",
        custom_metadata={},
        subject="Conference Badge",
    )
    fake.list_memories = AsyncMock(return_value=[hit])
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._list_impl()
    assert res["memories"][0]["subject"] == "Conference Badge"


@pytest.mark.asyncio
async def test_store_copies_subject_into_metadata():
    fake = FakeFlair()
    with patch.object(services, "get_memory_service", return_value=fake):
        res = await server._store_impl(
            "Receipt", "Total $58.40", image_url="/media/x.jpg"
        )
    assert res["status"] == "stored"
    kwargs = fake.add_memory.await_args.kwargs
    assert kwargs["subject"] == "Receipt"
    assert kwargs["custom_metadata"]["subject"] == "Receipt"
    assert kwargs["custom_metadata"]["image_url"] == "/media/x.jpg"


def test_viewable_image_url_signed_success(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET_NAME", "vault-bucket")
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    signed = "https://storage.googleapis.com/vault-bucket/vault-images/x.jpg?X-Goog-Signature=abc"
    with patch.object(server, "_signed_gcs_url", return_value=signed) as mock_sign:
        assert server.viewable_image_url("/media/x.jpg") == signed
    mock_sign.assert_called_once_with("vault-bucket", "x.jpg")


def test_viewable_image_url_sign_failure_no_dead_media(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET_NAME", "vault-bucket")
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    with patch.object(
        server, "_signed_gcs_url", side_effect=RuntimeError("ADC has no private key")
    ):
        assert server.viewable_image_url("/media/x.jpg") is None


def test_viewable_image_url_sign_failure_uses_proxy(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET_NAME", "vault-bucket")
    monkeypatch.setenv("VAULT_PROXY_URL", "https://proxy.example")
    with patch.object(
        server, "_signed_gcs_url", side_effect=RuntimeError("signBlob denied")
    ):
        assert (
            server.viewable_image_url("/media/x.jpg")
            == "https://proxy.example/media/x.jpg"
        )


def _fake_storage_client(seen, *, email, token, sign_error=None):
    class FakeBlob:
        def generate_signed_url(self, **kwargs):
            seen.update(kwargs)
            if sign_error is not None:
                raise sign_error
            return "https://storage.googleapis.com/signed"

    class FakeClient:
        def __init__(self):
            self._credentials = SimpleNamespace(
                valid=True, token=token, service_account_email=email
            )

        def bucket(self, name):
            assert name == "vault-bucket"

            def blob(path):
                assert path == "vault-images/x.jpg"
                return FakeBlob()

            return SimpleNamespace(blob=blob)

    return FakeClient()


def test_signed_gcs_url_uses_iam_signblob():
    seen = {}
    client = _fake_storage_client(
        seen,
        email="runtime-sa@example.iam.gserviceaccount.com",
        token="ya29.token",
    )
    with patch.object(server, "_storage_client", return_value=client):
        url = server._signed_gcs_url("vault-bucket", "x.jpg")
    assert url == "https://storage.googleapis.com/signed"
    assert seen["version"] == "v4"
    assert seen["method"] == "GET"
    assert seen["service_account_email"] == "runtime-sa@example.iam.gserviceaccount.com"
    assert seen["access_token"] == "ya29.token"


def test_signed_gcs_url_propagates_sign_failure():
    seen = {}
    client = _fake_storage_client(
        seen,
        email=None,
        token=None,
        sign_error=AttributeError("you need a private key to sign credentials"),
    )
    with patch.object(server, "_storage_client", return_value=client):
        with pytest.raises(AttributeError, match="private key"):
            server._signed_gcs_url("vault-bucket", "x.jpg")
