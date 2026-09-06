#!/usr/bin/env python
"""Peer agent querying the Visual Memory Vault over A2A.

Demonstrates the vault's A2A serving surface: a separate agent (this script)
discovers the vault through its agent card and asks a natural-language question,
e.g. "how much did I spend at Joe's Grill?". The vault answers from its Flair
memory over the open A2A protocol — no shared database, no direct API coupling.

Start the vault backend first:

    uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8000

Then query it:

    uv run python examples/a2a_peer_query.py "how much did I spend at Joe's Grill?"

Against a deployed agent, pass its base URL (Google credentials are attached
automatically when available), or the full agent-card URL:

    uv run python examples/a2a_peer_query.py --url https://<host> "<question>"
    uv run python examples/a2a_peer_query.py --card-url https://<host>/a2a/app/.well-known/agent-card.json "<question>"
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
)
from google.protobuf.json_format import ParseDict

_CARD_SUFFIX = "/.well-known/agent-card.json"


def a2a_base_url(base_url: str, app_name: str) -> str:
    """A2A RPC base for the vault agent, e.g. ``http://host:8000/a2a/app``."""
    return f"{base_url.rstrip('/')}/a2a/{app_name}"


def card_url_for(a2a_base: str) -> str:
    """Well-known agent-card URL for an A2A base."""
    return f"{a2a_base.rstrip('/')}{_CARD_SUFFIX}"


def a2a_base_from_card_url(card_url: str) -> str:
    """Recover the A2A base from a full agent-card URL."""
    if card_url.endswith(_CARD_SUFFIX):
        return card_url[: -len(_CARD_SUFFIX)]
    return card_url.rstrip("/")


def extract_text_parts(parts) -> list[str]:
    """Collect text from A2A message/artifact parts (dicts or objects)."""
    out: list[str] = []
    for part in parts or []:
        text = (
            part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        )
        if text:
            out.append(text)
    return out


def _auth_headers() -> dict[str, str]:
    """Best-effort Google bearer for deployed targets; just JSON headers locally."""
    headers = {"Content-Type": "application/json"}
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        headers["Authorization"] = f"Bearer {creds.token}"
    except Exception:
        pass  # local backend needs no auth
    return headers


async def _resolve_card(
    client: httpx.AsyncClient, card_url: str, a2a_base: str
) -> AgentCard:
    resp = await client.get(card_url)
    resp.raise_for_status()
    card = AgentCard()
    ParseDict(resp.json(), card, ignore_unknown_fields=True)
    # Force the transport URL to the base we resolved the card from, in case the
    # card advertises a relative or stale endpoint (mirrors the frontend proxy).
    if card.supported_interfaces:
        card.supported_interfaces[0].url = a2a_base
    else:
        card.supported_interfaces.add(url=a2a_base)
    return card


async def query_vault(
    question: str,
    *,
    a2a_base: str,
    card_url: str,
    context_id: str | None = None,
    timeout: float = 120,
) -> str:
    """Send one question to the vault agent over A2A and return its text reply."""
    async with httpx.AsyncClient(headers=_auth_headers(), timeout=timeout) as client:
        card = await _resolve_card(client, card_url, a2a_base)
        a2a_client = ClientFactory(ClientConfig(httpx_client=client)).create(card)
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=question)],
            context_id=context_id,
        )
        request = SendMessageRequest(
            message=message, configuration=SendMessageConfiguration()
        )
        texts: list[str] = []
        async for event in a2a_client.send_message(request):
            if hasattr(event, "HasField") and event.HasField("artifact_update"):
                texts.extend(extract_text_parts(event.artifact_update.artifact.parts))
        return "\n".join(texts) or "(no reply from the vault agent)"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query the Visual Memory Vault over the A2A protocol."
    )
    parser.add_argument("question", help="natural-language question for the vault")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="vault backend base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument("--app-name", default="app", help="ADK app name (default: app)")
    parser.add_argument(
        "--card-url",
        default=None,
        help="full agent-card URL (overrides --url/--app-name)",
    )
    args = parser.parse_args()

    if args.card_url:
        card_url = args.card_url
        a2a_base = a2a_base_from_card_url(card_url)
    else:
        a2a_base = a2a_base_url(args.url, args.app_name)
        card_url = card_url_for(a2a_base)

    reply = asyncio.run(
        query_vault(args.question, a2a_base=a2a_base, card_url=card_url)
    )
    print(reply)


if __name__ == "__main__":
    main()
