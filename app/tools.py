import uuid
from typing import Any

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.app_utils import services


async def store_visual_memory(
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

    if hasattr(mem_service, "_request"):
        try:
            body = {
                "id": mem_id,
                "agentId": getattr(mem_service, "_agent_id", "visual-memory-vault"),
                "subject": subject,
                "content": content,
                "durability": "persistent",
                "visibility": "shared",
                "tags": tags or ["adk:visual-memory-vault:user"],
            }
            res = await mem_service._request("PUT", f"/Memory/{mem_id}", json_body=body)
            return {
                "status": "success",
                "id": mem_id,
                "subject": subject,
                "output": res,
            }
        except Exception as exc:
            return {"status": "error", "message": f"Memory store failed: {exc}"}

    if hasattr(mem_service, "add_memory"):
        try:
            entry = MemoryEntry(
                id=mem_id,
                content=types.Content(role="model", parts=[types.Part(text=content)]),
            )
            await mem_service.add_memory(
                app_name="visual-memory-vault",
                user_id="user",
                memories=[entry],
                durability="persistent",
                visibility="shared",
            )
            return {"status": "success", "id": mem_id, "subject": subject}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return {
        "status": "error",
        "message": "Memory service does not support direct writes",
    }


async def search_visual_memories(query: str, limit: int = 5) -> dict[str, Any]:
    """Search stored visual memories and screenshot facts using semantic search via Flair.

    Args:
        query: The search question or keywords (e.g. 'hotel wifi password', 'book recommendations').
        limit: Maximum number of memory results to return.
    """
    mem_service = services.get_memory_service()
    if hasattr(mem_service, "search_memory"):
        try:
            res = await mem_service.search_memory(
                app_name="visual-memory-vault",
                user_id="user",
                query=query,
            )
            memories = []
            for m in res.memories[:limit]:
                text = ""
                if m.content and m.content.parts:
                    text = " ".join(p.text for p in m.content.parts if p.text)
                memories.append({"id": m.id, "content": text, "timestamp": m.timestamp})
            return {"status": "success", "results": memories}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return {"status": "error", "message": "Memory service does not support search"}


async def list_visual_memories() -> dict[str, Any]:
    """List all stored visual memories in the Flair memory bank."""
    mem_service = services.get_memory_service()
    if hasattr(mem_service, "_request"):
        try:
            records = await mem_service._request("GET", "/Memory/")
            return {"status": "success", "memories": records}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    if hasattr(mem_service, "search_memory"):
        try:
            res = await mem_service.search_memory(
                app_name="visual-memory-vault",
                user_id="user",
                query="*",
            )
            return {
                "status": "success",
                "memories": [
                    {
                        "id": m.id,
                        "content": " ".join(p.text for p in m.content.parts if p.text),
                    }
                    for m in res.memories
                    if m.content and m.content.parts
                ],
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    return {"status": "error", "message": "Memory service unavailable"}
