from typing import Any

from app.flair_client import list_memories, search_memories, store_memory


def store_visual_memory(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Store a photo, screenshot, or visual information into the agent's FLAIR memory bank.

    Args:
        subject: One-line title or main topic of the photo/screenshot (e.g. 'Hotel WiFi Keycard', 'Lunch Receipt').
        description: Detailed extracted information, text/OCR content, or context to remember.
        tags: Optional list of category tags (e.g. ['wifi', 'hotel', 'receipt', 'travel']).
        image_url: Optional public URL of the original stored image file.
    """
    content = description
    if image_url:
        content = f"{description}\nOriginal Image: {image_url}"
    return store_memory(subject=subject, content=content, tags=tags)


def search_visual_memories(query: str, limit: int = 5) -> dict[str, Any]:
    """Search stored visual memories and screenshot facts using semantic search via FLAIR.

    Args:
        query: The search question or keywords (e.g. 'hotel wifi password', 'book recommendations').
        limit: Maximum number of memory results to return.
    """
    return search_memories(query=query, limit=limit)


def list_visual_memories() -> dict[str, Any]:
    """List all stored visual memories in the FLAIR memory bank."""
    return list_memories()
