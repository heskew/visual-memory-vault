# Cloud Run image for the Visual Memory Vault MCP server (Streamable HTTP).
# Front it with an OAuth layer before exposing it to remote MCP clients.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./
COPY ./app ./app
COPY ./mcp_server ./mcp_server

RUN uv sync --extra mcp

EXPOSE 8080

CMD ["uv", "run", "python", "-m", "mcp_server.server", "--http", "--host", "0.0.0.0", "--port", "8080"]
