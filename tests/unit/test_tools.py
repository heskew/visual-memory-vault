from unittest.mock import AsyncMock, MagicMock, patch

from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app import tools
from app.app_utils import services


def test_store_visual_memory_success():
    mock_service = MagicMock()
    mock_service.add_memory = AsyncMock()

    with patch.object(services, "get_memory_service", return_value=mock_service):
        res = tools.store_visual_memory(
            subject="Receipt",
            description="Coffee $5.00",
            tags=["cafe"],
            image_url="/media/coffee.jpg",
        )
        assert res["status"] == "success"
        assert res["subject"] == "Receipt"
        assert "id" in res
        mock_service.add_memory.assert_awaited_once()


def test_search_visual_memories_success():
    mock_service = MagicMock()
    mock_entry = MemoryEntry(
        id="mem-1",
        content=types.Content(
            role="model", parts=[types.Part(text="Subject: Wifi\nPassword: 123")]
        ),
        timestamp="2026-08-22T00:00:00Z",
    )
    mock_service.search_memory = AsyncMock(
        return_value=SearchMemoryResponse(memories=[mock_entry])
    )

    with patch.object(services, "get_memory_service", return_value=mock_service):
        res = tools.search_visual_memories("Wifi")
        assert res["status"] == "success"
        assert len(res["results"]) == 1
        assert res["results"][0]["id"] == "mem-1"
        assert "Password: 123" in res["results"][0]["content"]


def test_list_visual_memories_success():
    mock_service = MagicMock()
    mock_service._request = AsyncMock(return_value=[{"id": "1", "subject": "Test"}])

    with patch.object(services, "get_memory_service", return_value=mock_service):
        res = tools.list_visual_memories()
        assert res["status"] == "success"
        assert len(res["memories"]) == 1
        assert res["memories"][0]["id"] == "1"
