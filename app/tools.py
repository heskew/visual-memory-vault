import asyncio
import logging
import uuid
from typing import Any

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.app_utils import services

logger = logging.getLogger(__name__)


def _run_coro(coro):
    """Run an async coroutine safely from synchronous tool functions."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import nest_asyncio

    nest_asyncio.apply()
    return loop.run_until_complete(coro)


def store_visual_memory(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Store a photo, screenshot, or visual information into the agent's Flair memory bank.

    Args:
        subject: One-line title or main topic of the photo/screenshot (e.g. 'Hotel WiFi Keycard', 'Lunch Receipt').
        description: Detailed extracted information, text/OCR content, or context to remember.
        tags: Optional list of category tags (e.g. ['wifi', 'hotel', 'receipt', 'travel']).
        image_url: Optional public URL of the original stored image file.
    """
    mem_service = services.get_memory_service()
    mem_id = str(uuid.uuid4())
    content = f"Subject: {subject}\n{description}"
    if image_url:
        content += f"\nOriginal Image: {image_url}"

    entry = MemoryEntry(
        id=mem_id,
        content=types.Content(role="model", parts=[types.Part(text=content)]),
    )

    try:
        _run_coro(
            mem_service.add_memory(
                app_name="visual-memory-vault",
                user_id="user",
                memories=[entry],
                durability="persistent",
                visibility="shared",
            )
        )
        return {
            "status": "success",
            "id": mem_id,
            "subject": subject,
        }
    except Exception as exc:
        logger.error("store_visual_memory error: %s", exc)
        return {"status": "error", "message": f"Memory store failed: {exc}"}


def search_visual_memories(query: str, limit: int = 5) -> dict[str, Any]:
    """Search stored visual memories and screenshot facts using semantic search via Flair.

    Args:
        query: The search question or keywords (e.g. 'hotel wifi password', 'book recommendations').
        limit: Maximum number of memory results to return.
    """
    mem_service = services.get_memory_service()
    try:
        resp = _run_coro(
            mem_service.search_memory(
                app_name="visual-memory-vault",
                user_id="user",
                query=query,
            )
        )
        results = []
        for m in resp.memories[:limit]:
            text = ""
            if m.content and m.content.parts:
                text = " ".join(p.text for p in m.content.parts if p.text)
            results.append(
                {
                    "id": m.id,
                    "content": text,
                    "timestamp": m.timestamp,
                }
            )
        return {"status": "success", "results": results}
    except Exception as exc:
        logger.error("search_visual_memories error: %s", exc)
        return {"status": "error", "message": str(exc)}


def list_visual_memories(limit: int = 20) -> dict[str, Any]:
    """List recent stored visual memories in the Flair memory bank."""
    # Searches broadly across the app+user scope using semantic search
    return search_visual_memories(query="*", limit=limit)
