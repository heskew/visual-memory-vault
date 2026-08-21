from app.agent import app, root_agent
from app.tools import (
    list_visual_memories,
    search_visual_memories,
    store_visual_memory,
)


def test_agent_configuration():
    assert root_agent.name == "root_agent"
    assert "Visual Memory Vault" in root_agent.instruction
    assert "store_visual_memory" in root_agent.instruction

    tool_funcs = [t if callable(t) else getattr(t, "func", t) for t in root_agent.tools]
    assert store_visual_memory in tool_funcs
    assert search_visual_memories in tool_funcs
    assert list_visual_memories in tool_funcs


def test_app_structure():
    assert app.name == "app"
    assert app.root_agent is root_agent
