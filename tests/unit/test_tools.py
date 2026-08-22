from unittest.mock import patch

from app import tools


def test_store_visual_memory_success():
    with patch.object(
        tools, "_sync_request", return_value={"id": "1", "written": True}
    ):
        res = tools.store_visual_memory(
            subject="Receipt",
            description="Coffee $5.00",
            tags=["cafe"],
            image_url="/media/coffee.jpg",
        )
        assert res["status"] == "success"
        assert res["subject"] == "Receipt"
        assert "id" in res


def test_search_visual_memories_success():
    records = [
        {
            "id": "mem-1",
            "subject": "Wifi",
            "content": "Subject: Wifi\nPassword: 123",
            "createdAt": "2026-08-22T00:00:00Z",
        }
    ]
    with patch.object(tools, "_sync_request", return_value=records):
        res = tools.search_visual_memories("Wifi")
        assert res["status"] == "success"
        assert len(res["results"]) == 1
        assert res["results"][0]["id"] == "mem-1"
        assert "Password: 123" in res["results"][0]["content"]


def test_list_visual_memories_success():
    records = [{"id": "1", "subject": "Test"}]
    with patch.object(tools, "_sync_request", return_value=records):
        res = tools.list_visual_memories()
        assert res["status"] == "success"
        assert len(res["memories"]) == 1
        assert res["memories"][0]["id"] == "1"
