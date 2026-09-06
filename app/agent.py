import asyncio
import functools
from typing import Any

from adk_flair import FlairMemoryService
from adk_flair.tools import create_flair_tools
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models import Gemini
from google.genai import types

from app.app_utils import services
from app.app_utils.memory_ids import stable_memory_id

MODEL = "gemini-3.7-flash"


class _SignatureFlair(FlairMemoryService):
    """Import-time stand-in so create_flair_tools can bind signatures.

    CI and unit collection often have no Flair keyfile, so
    ``get_memory_service()`` falls back to ``InMemoryMemoryService``.
    ``create_flair_tools`` rejects that type. Runtime wrappers re-bind
    against the live service via ``_get_runtime_tools()``.
    """

    def __init__(self) -> None:
        pass


def _bind_tool_signatures():
    svc = services.get_memory_service()
    if not isinstance(svc, FlairMemoryService):
        svc = _SignatureFlair()
    return create_flair_tools(svc, app_name="visual-memory-vault", user_id="user")


_async_store, _async_search, _async_list = _bind_tool_signatures()


def _run_sync(coro):
    """Safely run an async tool coroutine from synchronous runner threads."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import nest_asyncio

    nest_asyncio.apply()
    return loop.run_until_complete(coro)


def _get_runtime_tools():
    svc = services.get_memory_service()
    return create_flair_tools(svc, app_name="visual-memory-vault", user_id="user")


async def _store_memory_impl(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    custom_metadata: dict | None = None,
) -> dict:
    if not description or not description.strip():
        return {"error": "description must be non-empty - provide the memory text"}
    metadata = dict(custom_metadata) if custom_metadata is not None else None
    if tags is not None:
        if metadata is None:
            metadata = {}
        metadata["tags"] = list(tags)
    entry = MemoryEntry(
        id=stable_memory_id(metadata),
        content=types.Content(role="user", parts=[types.Part(text=description)]),
    )
    try:
        await services.get_memory_service().add_memory(
            app_name="visual-memory-vault",
            user_id="user",
            memories=[entry],
            custom_metadata=metadata,
            subject=subject,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"status": "stored", "subject": subject}


def store_memory(
    subject: str,
    description: str,
    tags: list[str] | None = None,
    custom_metadata: dict | None = None,
) -> dict:
    """Save a memory to the user's long-term Flair memory.

    Storing the same image again (same image_url in custom_metadata) updates the
    one record rather than creating a duplicate.

    Args:
        subject: Short human-readable title for the memory.
        description: The full text of the memory, understandable on its own.
        tags: Optional short category labels, e.g. ["receipt", "travel"].
        custom_metadata: Optional structured attributes stored verbatim, e.g.
            {"merchant": "...", "amount": "...", "image_url": "/media/x.jpg"}.

    Returns:
        {"status": "stored", "subject": <subject>} on success, or
        {"error": <message>} on failure.
    """
    return _run_sync(_store_memory_impl(subject, description, tags, custom_metadata))


@functools.wraps(_async_search)
def search_memory(*args: Any, **kwargs: Any) -> dict:
    tools = _get_runtime_tools()
    return _run_sync(tools[1](*args, **kwargs))


@functools.wraps(_async_list)
def list_memories(*args: Any, **kwargs: Any) -> dict:
    tools = _get_runtime_tools()
    return _run_sync(tools[2](*args, **kwargs))


flair_tools = [store_memory, search_memory, list_memories]

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Visual Memory Vault agent. You help users store, extract, "
        "and retrieve visual information, photo details, receipts, documents, "
        "screenshots, and context using Flair long-term memory.\n\n"
        "CRITICAL INSTRUCTIONS FOR UPLOADED PHOTOS & SCREENSHOTS:\n"
        "1. When an image is uploaded or provided, examine the image thoroughly using "
        "your multimodal capabilities and extract all key text, numbers, items, dates, "
        "locations, and visual context.\n"
        "2. You MUST IMMEDIATELY call the `store_memory` tool to save these extracted "
        "details into Flair memory. Do not ask for confirmation before storing.\n"
        "3. Set `subject` to a concise title (e.g. 'Starbucks Receipt - $6.50', 'Hotel WiFi Info'), "
        "set `description` to the detailed extracted facts and text context, and pass "
        "`custom_metadata` with at least `image_url` when a media path is provided.\n"
        "4. If the image is a receipt or invoice, you MUST extract merchant, amount, currency, "
        "and date, and pass them in `custom_metadata` together with image_url: "
        "`custom_metadata={'merchant': '...', 'amount': '...', 'currency': 'USD', "
        "'date': 'YYYY-MM-DD', 'image_url': '<path>'}`. Keep the prose description.\n"
        "5. In your response to the user, summarize what was saved and highlight key details. "
        "For receipts, also include one machine-readable line:\n"
        'RECEIPT: {"merchant":"...","amount":"...","currency":"...","date":"..."}\n\n'
        "When users ask to recall, find, or browse memories, use `search_memory` or `list_memories`."
    ),
    tools=flair_tools,
)

app = App(
    root_agent=root_agent,
    name="app",
)
