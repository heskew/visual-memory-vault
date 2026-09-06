# Visual Memory Vault — MCP server

Exposes the vault to the agents you already use, as MCP tools, so you don't need
a separate app. Recall is Flair semantic search scoped to the vault; image hits
are enriched with a time-limited signed GCS URL so the image opens without the
proxy API key. It depends only on Flair (recall) and GCS (images) — no ADK or
Gemini — so it stays small and hosts anywhere.

## Tools

- `search_vault(query, limit)` — semantic search over the vault.
- `list_vault(limit, offset)` — recent memories, newest first.
- `store_vault(subject, description, tags?, image_url?)` — add a memory
  (idempotent per `image_url`).

## Environment

The server must use the same Flair identity the vault writes under, so it can see
the vault's memories:

- `FLAIR_URL`, `FLAIR_AGENT_ID=visual-memory-vault`, and `FLAIR_KEYFILE` (or
  `FLAIR_PRIVATE_KEY_B64` in containers).
- `GCS_BUCKET_NAME` to mint signed image URLs; or `VAULT_PROXY_URL` to return
  proxy `/media` links; otherwise the stored reference is returned as-is.
- `VAULT_SIGNED_URL_TTL_MIN` (default 60).

## Use it from Claude Code (local, stdio — works today)

```bash
claude mcp add vault -- uv run python -m mcp_server.server
```

Or add to `.mcp.json`:

```json
{ "mcpServers": { "vault": { "command": "uv", "args": ["run", "python", "-m", "mcp_server.server"] } } }
```

Then ask Claude Code things like "search my vault for the hotel wifi password."

## Google-native hosting + registration

### 1. Host on Cloud Run

The server serves Streamable HTTP with `--http`. Build and deploy it as its own
Cloud Run service (the ADK agent uses `agents-cli deploy`; this is a separate
service), with the Flair key in Secret Manager:

```bash
gcloud run deploy vault-mcp --source . \
  --region us-east1 \
  --set-env-vars "FLAIR_URL=...,FLAIR_AGENT_ID=visual-memory-vault,GCS_BUCKET_NAME=..." \
  --set-secrets "FLAIR_PRIVATE_KEY_B64=vault-flair-key:latest"
```

(Point the build at `mcp_server/Dockerfile`, or copy it to the build root.)

### 2. Register it in Agent Registry (Google's MCP catalog)

Agent Registry is Google Cloud's fleet-wide catalog of agents and MCP servers.
Export the tool spec and register the server so Google/Gemini agents can use it:

```bash
uv run python -m mcp_server.export_toolspec > toolspec.json
gcloud agent-registry services create visual-memory-vault \
  --location=us-east1 \
  --mcp-server-spec-type=tool-spec --mcp-server-spec-content=toolspec.json \
  --interfaces="url=https://vault-mcp-....run.app/mcp,protocolBinding=jsonrpc"
```

A `google_agent_registry_service` Terraform resource with an `mcp_server_spec`
block does the same in the project's `deployment/terraform/`.

## The Claude phone app

Claude's remote connectors expect an OAuth 2.0 flow. Cloud Run does not offer
managed OAuth (Google's managed OAuth is the Agent Runtime / Gemini Enterprise
path), so a public Claude connector needs an OAuth layer in front of the Cloud
Run service. Until that's in place, Claude Code on the laptop uses this server
over stdio, and the Gemini side reaches it through Agent Registry.
