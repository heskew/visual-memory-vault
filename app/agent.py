import asyncio
import functools
from typing import Any

from adk_flair.tools import create_flair_tools
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.app_utils import services

MODEL = "gemini-3.7-flash"

_async_store, _async_search, _async_list = create_flair_tools(
    services.get_memory_service(),
    app_name="visual-memory-vault",
    user_id="user",
)


def _run_sync(coro):
    """Safely run an async tool coroutine from synchronous runner threads."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import nest_asyncio

    nest_asyncio.apply()
    return loop.run_until_complete(coro)


@functools.wraps(_async_store)
def store_memory(*args: Any, **kwargs: Any) -> dict:
    return _run_sync(_async_store(*args, **kwargs))


@functools.wraps(_async_search)
def search_memory(*args: Any, **kwargs: Any) -> dict:
    return _run_sync(_async_search(*args, **kwargs))


@functools.wraps(_async_list)
def list_memories(*args: Any, **kwargs: Any) -> dict:
    return _run_sync(_async_list(*args, **kwargs))


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
        "`custom_metadata={'image_url': '<path>'}` if a media path is provided.\n"
        "4. In your response to the user, summarize what was saved and highlight key details.\n\n"
        "When users ask to recall, find, or browse memories, use `search_memory` or `list_memories`."
    ),
    tools=flair_tools,
)

app = App(
    root_agent=root_agent,
    name="app",
)
