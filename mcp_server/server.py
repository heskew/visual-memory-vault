"""MCP server exposing the Visual Memory Vault to any MCP-speaking agent.

Lets the agents you already use (Claude Code, the Claude app via a hosted
connector, or any MCP client) search and add to your vault as tools. Recall is
Flair semantic search scoped to the vault; image hits are enriched with a
time-limited signed GCS URL so the image is viewable without the proxy API key.
No ADK or Gemini dependency — just Flair (recall) and GCS (images).

Local (Claude Code / Claude Desktop, stdio):

    uv run python -m mcp_server.server

Remote (a hosted connector for the Claude phone app); put OAuth in front when
you expose it publicly:

    uv run python -m mcp_server.server --http --host 0.0.0.0 --port 8080

Environment: the same Flair identity the vault writes under, so the server sees
the vault's memories — ``FLAIR_URL``, ``FLAIR_AGENT_ID=visual-memory-vault``,
``FLAIR_KEYFILE`` (or ``FLAIR_PRIVATE_KEY_B64``). For viewable images set
``GCS_BUCKET_NAME`` (signed URLs) or ``VAULT_PROXY_URL`` (proxy links).
"""

from __future__ import annotations

import argparse
import os
from datetime import timedelta

from adk_flair import FlairMemoryService
from mcp.server.mcpserver import MCPServer

from app.app_utils import services
from app.app_utils.memory_ids import stable_memory_id

APP_NAME = "visual-memory-vault"
USER_ID = "user"
_SIGNED_URL_TTL_MIN = int(os.environ.get("VAULT_SIGNED_URL_TTL_MIN", "60"))

server = MCPServer(
    "visual-memory-vault",
    instructions=(
        "Search and store the user's visual memory vault: receipts, screenshots, "
        "documents, and photos, with their extracted text and details."
    ),
)


def _image_name(image_url: str | None) -> str | None:
    if not image_url:
        return None
    return image_url.rstrip("/").rsplit("/", 1)[-1] or None


def viewable_image_url(image_url: str | None) -> str | None:
    """Turn a stored ``/media/<name>`` reference into something openable.

    Prefers a time-limited signed GCS URL. Falls back to a proxy URL when
    ``VAULT_PROXY_URL`` is set, else returns the stored reference unchanged.
    """
    name = _image_name(image_url)
    if not name:
        return image_url
    bucket = os.environ.get("GCS_BUCKET_NAME")
    if bucket:
        try:
            from google.cloud import storage

            blob = storage.Client().bucket(bucket).blob(f"vault-images/{name}")
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=_SIGNED_URL_TTL_MIN),
                method="GET",
            )
        except Exception:
            pass  # fall through to proxy/raw
    base = os.environ.get("VAULT_PROXY_URL")
    if base:
        return f"{base.rstrip('/')}/media/{name}"
    return image_url


def _format(entry) -> dict:
    text = FlairMemoryService._extract_content_text(entry.content) or ""
    meta = dict(entry.custom_metadata or {})
    image_url = meta.get("image_url")
    return {
        "id": entry.id,
        "subject": meta.get("subject"),
        "content": text,
        "timestamp": entry.timestamp,
        "image_url": viewable_image_url(image_url) if image_url else None,
        "merchant": meta.get("merchant"),
        "amount": meta.get("amount"),
        "currency": meta.get("currency"),
        "date": meta.get("date"),
    }


async def _search_impl(query: str, limit: int = 5) -> dict:
    svc = services.get_memory_service()
    resp = await svc.search_memory(app_name=APP_NAME, user_id=USER_ID, query=query)
    hits = [_format(m) for m in resp.memories[:limit]]
    return {"count": len(hits), "memories": hits}


async def _list_impl(limit: int = 20, offset: int = 0) -> dict:
    svc = services.get_memory_service()
    entries = await svc.list_memories(
        app_name=APP_NAME, user_id=USER_ID, limit=limit, offset=offset
    )
    hits = [_format(e) for e in entries]
    return {"count": len(hits), "offset": offset, "memories": hits}


async def _store_impl(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    image_url: str | None = None,
) -> dict:
    from google.adk.memory.memory_entry import MemoryEntry
    from google.genai import types

    if not description or not description.strip():
        return {"error": "description must be non-empty"}
    metadata: dict = {}
    if image_url:
        metadata["image_url"] = image_url
    if tags:
        metadata["tags"] = list(tags)
    entry = MemoryEntry(
        id=stable_memory_id(metadata or None),
        content=types.Content(role="user", parts=[types.Part(text=description)]),
    )
    try:
        await services.get_memory_service().add_memory(
            app_name=APP_NAME,
            user_id=USER_ID,
            memories=[entry],
            custom_metadata=metadata or None,
            subject=subject,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"status": "stored", "subject": subject}


@server.tool()
async def search_vault(query: str, limit: int = 5) -> dict:
    """Search the visual memory vault by meaning.

    Use for questions like "how much did I spend at Joe's Grill?" or "what was
    the hotel wifi password?". Returns matching memories with their extracted
    text and, when present, a viewable image link.

    Args:
        query: What to look for, in natural language.
        limit: Maximum number of memories to return (default 5).
    """
    return await _search_impl(query, limit)


@server.tool()
async def list_vault(limit: int = 20, offset: int = 0) -> dict:
    """List recent vault memories, newest first.

    Args:
        limit: Page size (default 20).
        offset: How many newest memories to skip (default 0).
    """
    return await _list_impl(limit, offset)


@server.tool()
async def store_vault(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    image_url: str | None = None,
) -> dict:
    """Store a memory in the vault.

    Storing again with the same image_url updates the one record rather than
    creating a duplicate.

    Args:
        subject: Short title for the memory.
        description: The full text to remember.
        tags: Optional category labels.
        image_url: Optional stored image reference the memory is about.
    """
    return await _store_impl(subject, description, tags, image_url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Visual Memory Vault MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve Streamable HTTP (for remote hosting) instead of stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
