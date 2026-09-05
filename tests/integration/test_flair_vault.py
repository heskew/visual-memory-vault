import pytest

from app.agent import list_memories, search_memory, store_memory


def test_flair_vault_end_to_end():
    """Store, search, and list a visual memory through the agent's real tools.

    Exercises the same ``store_memory`` / ``search_memory`` / ``list_memories``
    wrappers the agent calls (``app.agent``), bound to the live Flair service.
    Requires a reachable Flair daemon: set ``FLAIR_URL`` and the agent identity
    from the Quickstart. ``create_flair_tools`` rejects a non-Flair service, so
    this skips when no Flair keyfile is configured.
    """
    store_res = store_memory(
        subject="Conference Badge",
        description=(
            "Attendee: Nathan Heskew, Event: Build with Gemini, Role: Speaker"
        ),
        tags=["event", "gemini", "badge"],
        custom_metadata={"image_url": "/media/badge.jpg"},
    )
    if store_res.get("status") not in ("stored", "success"):
        pytest.skip(f"Flair not reachable for integration test: {store_res!r}")

    search_res = search_memory("Nathan Heskew Build with Gemini")
    assert "memories" in search_res

    list_res = list_memories()
    assert "memories" in list_res
