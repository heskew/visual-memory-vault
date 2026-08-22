from app.agent import app, root_agent


def test_agent_configuration():
    assert root_agent.name == "root_agent"
    assert "Visual Memory Vault" in root_agent.instruction
    assert "store_memory" in root_agent.instruction

    tool_names = [getattr(t, "__name__", str(t)) for t in root_agent.tools]
    assert "store_memory" in tool_names
    assert "search_memory" in tool_names
    assert "list_memories" in tool_names


def test_app_structure():
    assert app.name == "app"
    assert app.root_agent is root_agent
