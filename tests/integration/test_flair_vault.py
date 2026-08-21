from app.tools import list_visual_memories, search_visual_memories, store_visual_memory


def test_flair_vault_end_to_end():
    """Verify that visual memories can be stored, listed, and searched against Flair."""
    store_res = store_visual_memory(
        subject="Conference Badge",
        description="Attendee: Nathan Heskew, Event: Build with Gemini, Role: Speaker",
        tags=["event", "gemini", "badge"],
        image_url="/media/badge.jpg",
    )
    assert store_res["status"] == "success"

    search_res = search_visual_memories(
        query="Nathan Heskew Build with Gemini", limit=3
    )
    assert search_res["status"] == "success"

    list_res = list_visual_memories()
    assert list_res["status"] == "success"
