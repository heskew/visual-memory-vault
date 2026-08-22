from adk_flair.tools import create_flair_tools
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.app_utils import services

MODEL = "gemini-3.7-flash"

flair_tools = create_flair_tools(
    services.get_memory_service(),
    app_name="visual-memory-vault",
    user_id="user",
)

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
