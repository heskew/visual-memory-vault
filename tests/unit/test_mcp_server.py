import hashlib
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
