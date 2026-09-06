import hashlib
from unittest.mock import AsyncMock, patch

from adk_flair import FlairMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.agent import list_memories, search_memory, store_memory
from app.app_utils import services


class FakeFlair(FlairMemoryService):
    """FlairMemoryService stand-in that skips keyfile/URL construction."""

    def __init__(self):
        self.add_memory = AsyncMock(return_value=None)
        self.search_memory = AsyncMock(return_value=SearchMemoryResponse(memories=[]))
        self.list_memories = AsyncMock(return_value=[])


def _entry(mem_id: str, text: str, subject: str | None = None) -> MemoryEntry:
    return MemoryEntry(
        id=mem_id,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        timestamp="2026-08-22T00:00:00Z",
        custom_metadata={"subject": subject} if subject else {},
    )


def test_store_memory_passes_receipt_custom_metadata():
    fake = FakeFlair()
    metadata = {
        "merchant": "Joe's Grill",
        "amount": "58.40",
        "currency": "USD",
        "date": "2026-08-20",
        "image_url": "/media/receipt.jpg",
    }

    with patch.object(services, "get_memory_service", return_value=fake):
        res = store_memory(
            subject="Joe's Grill Receipt - $58.40",
            description="Dinner at Joe's Grill. Ribeye and sparkling water. Total $58.40.",
            tags=["receipt"],
            custom_metadata=metadata,
        )

    assert res["status"] == "stored"
    assert res["subject"] == "Joe's Grill Receipt - $58.40"
    fake.add_memory.assert_awaited_once()
    kwargs = fake.add_memory.await_args.kwargs
    stored = kwargs["custom_metadata"]
    assert stored["merchant"] == "Joe's Grill"
    assert stored["amount"] == "58.40"
    assert stored["currency"] == "USD"
    assert stored["date"] == "2026-08-20"
    assert stored["image_url"] == "/media/receipt.jpg"
    assert stored["tags"] == ["receipt"]
    assert stored["subject"] == "Joe's Grill Receipt - $58.40"
    assert kwargs["subject"] == "Joe's Grill Receipt - $58.40"


def test_search_memory_returns_live_shape():
    fake = FakeFlair()
    fake.search_memory = AsyncMock(
        return_value=SearchMemoryResponse(
            memories=[_entry("mem-1", "Password: 123", subject="Wifi")]
        )
    )

    with patch.object(services, "get_memory_service", return_value=fake):
        res = search_memory("Wifi")

    assert res["count"] == 1
    assert res["memories"][0]["id"] == "mem-1"
    assert "Password: 123" in res["memories"][0]["content"]
    assert res["memories"][0]["custom_metadata"]["subject"] == "Wifi"


def test_list_memories_returns_live_shape():
    fake = FakeFlair()
    fake.list_memories = AsyncMock(
        return_value=[_entry("mem-1", "Subject: Test", subject="Test")]
    )

    with patch.object(services, "get_memory_service", return_value=fake):
        res = list_memories()

    assert res["count"] == 1
    assert res["memories"][0]["id"] == "mem-1"
    assert res["memories"][0]["subject"] == "Test"


def _stored_entry(fake):
    return fake.add_memory.await_args.kwargs["memories"][0]


def test_store_memory_uses_stable_id_from_image_url():
    fake = FakeFlair()
    meta = {"image_url": "/media/receipt.jpg"}
    expected = hashlib.sha256(b"vault:/media/receipt.jpg").hexdigest()[:32]

    with patch.object(services, "get_memory_service", return_value=fake):
        store_memory(
            "Receipt", "Total $58.40 at Joe's Grill.", custom_metadata=dict(meta)
        )
        first_id = _stored_entry(fake).id
        # A retry whose description drifts must still hit the same record id.
        store_memory(
            "Receipt", "Dinner totalling 58.40 dollars.", custom_metadata=dict(meta)
        )
        second_id = _stored_entry(fake).id

    assert first_id == expected
    assert second_id == expected


def test_store_memory_without_key_falls_back_to_content_hash():
    fake = FakeFlair()
    with patch.object(services, "get_memory_service", return_value=fake):
        store_memory("Note", "A chat-originated memory with no image.")
    # id None -> adk-flair hashes the content itself (no regression).
    assert _stored_entry(fake).id is None
