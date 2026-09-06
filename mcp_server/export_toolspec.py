"""Emit the MCP server's tool spec for Google Agent Registry.

Agent Registry does not introspect an external MCP server; you upload its
``tools/list`` response (max 10 KB). This prints that JSON so you can register:

    uv run python -m mcp_server.export_toolspec > toolspec.json
    gcloud agent-registry services create visual-memory-vault \\
      --location=us-east1 --mcp-server-spec-type=tool-spec \\
      --mcp-server-spec-content=toolspec.json \\
      --interfaces="url=$MCP_URL,protocolBinding=jsonrpc"
"""

from __future__ import annotations

import asyncio
import json

from mcp_server.server import server


def _tool_dict(tool) -> dict:
    out = {"name": tool.name}
    desc = getattr(tool, "description", None)
    if desc:
        out["description"] = desc
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if schema is not None:
        out["inputSchema"] = schema
    return out


async def _tools() -> list[dict]:
    tools = await server.list_tools()
    return [_tool_dict(t) for t in tools]


def main() -> None:
    print(json.dumps({"tools": asyncio.run(_tools())}, indent=2))


if __name__ == "__main__":
    main()
