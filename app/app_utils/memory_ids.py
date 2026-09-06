"""Deterministic Flair record ids for idempotent vault writes.

Shared by the ADK agent's store tool (``app/agent.py``) and the MCP server
(``mcp_server/server.py``) so both write paths derive the same record id for a
given image. adk-flair upserts a memory by record id and otherwise hashes the
content, so keying the id on the stable ``image_url`` keeps re-ingestion
idempotent no matter which path (or retry) does the write.
"""

from __future__ import annotations

import hashlib


def stable_memory_id(custom_metadata: dict | None) -> str | None:
    """Record id derived from a stable per-image key, or None to fall back.

    Returns None when no stable key is present, preserving adk-flair's
    content-hash behavior (e.g. chat-originated stores with no image).
    """
    if not custom_metadata:
        return None
    key = custom_metadata.get("idempotency_key") or custom_metadata.get("image_url")
    if not key:
        return None
    return hashlib.sha256(f"vault:{key}".encode()).hexdigest()[:32]
