from unittest.mock import patch

from app.tools import (
    list_visual_memories,
    search_visual_memories,
    store_visual_memory,
)


def test_store_visual_memory_basic():
    with patch("app.tools.store_memory") as mock_store:
        mock_store.return_value = {"status": "success", "id": "test-1"}
        res = store_visual_memory(
            subject="Lunch Receipt",
            description="Total: $15.50 at Chipotle",
            tags=["receipt", "food"],
        )
        assert res["status"] == "success"
        mock_store.assert_called_once_with(
            subject="Lunch Receipt",
            content="Total: $15.50 at Chipotle",
            tags=["receipt", "food"],
        )


def test_store_visual_memory_with_image_url():
    with patch("app.tools.store_memory") as mock_store:
        mock_store.return_value = {"status": "success", "id": "test-2"}
        res = store_visual_memory(
            subject="Hotel Keycard",
            description="Room 404, WiFi: guest123",
            image_url="/media/hotel_key.jpg",
        )
        assert res["status"] == "success"
        mock_store.assert_called_once_with(
            subject="Hotel Keycard",
            content="Room 404, WiFi: guest123\nOriginal Image: /media/hotel_key.jpg",
            tags=None,
        )


def test_search_visual_memories():
    with patch("app.tools.search_memories") as mock_search:
        mock_search.return_value = {"status": "success", "results": "WiFi: guest123"}
        res = search_visual_memories(query="hotel wifi", limit=3)
        assert res["status"] == "success"
        mock_search.assert_called_once_with(query="hotel wifi", limit=3)


def test_list_visual_memories():
    with patch("app.tools.list_memories") as mock_list:
        mock_list.return_value = {"status": "success", "memories": "All memories"}
        res = list_visual_memories()
        assert res["status"] == "success"
        mock_list.assert_called_once()
