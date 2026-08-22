import logging
import uuid
from typing import Any

import httpx
from adk_flair.memory_service import _sign_request

from app.app_utils import services

logger = logging.getLogger(__name__)


def _sync_request(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Execute a synchronous signed request to Harper Fabric / Flair."""
    svc = services.get_memory_service()
    if not hasattr(svc, "_private_key") or not hasattr(svc, "_url"):
        raise RuntimeError("Flair memory service credentials unavailable")

    auth = _sign_request(svc._private_key, svc._agent_id, method, path)
    headers = {"Authorization": auth, "Content-Type": "application/json"}

    with httpx.Client(base_url=svc._url, timeout=30.0) as client:
        resp = client.request(
            method=method, url=path, json=json_body, params=params, headers=headers
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}


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
    svc = services.get_memory_service()
    agent_id = getattr(svc, "_agent_id", "visual-memory-vault")
    mem_id = str(uuid.uuid4())
    content = f"Subject: {subject}\n{description}"
    if image_url:
        content += f"\nOriginal Image: {image_url}"

    body = {
        "id": mem_id,
        "agentId": agent_id,
        "subject": subject,
        "content": content,
        "durability": "persistent",
        "visibility": "shared",
        "tags": tags or ["adk:visual-memory-vault:user"],
    }

    try:
        res = _sync_request("POST", "/Memory/", json_body=body)
        return {
            "status": "success",
            "id": res.get("id") or mem_id,
            "subject": subject,
            "output": res,
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
    try:
        # Search via GET /Memory/?q=... or vector search
        records = _sync_request("GET", "/Memory/")
        matched = []
        q_lower = query.lower()
        for r in records:
            text = f"{r.get('subject', '')} {r.get('content', '')}".lower()
            if any(term in text for term in q_lower.split()):
                matched.append(
                    {
                        "id": r.get("id"),
                        "subject": r.get("subject"),
                        "content": r.get("content"),
                        "createdAt": r.get("createdAt"),
                    }
                )
        return {"status": "success", "results": matched[:limit]}
    except Exception as exc:
        logger.error("search_visual_memories error: %s", exc)
        return {"status": "error", "message": str(exc)}


def list_visual_memories() -> dict[str, Any]:
    """List all stored visual memories in the Flair memory bank."""
    try:
        records = _sync_request("GET", "/Memory/")
        return {"status": "success", "memories": records}
    except Exception as exc:
        logger.error("list_visual_memories error: %s", exc)
        return {"status": "error", "message": str(exc)}
