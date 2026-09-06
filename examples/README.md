# Examples

## A2A peer query (`a2a_peer_query.py`)

A standalone peer agent that queries the Visual Memory Vault over the open
[A2A protocol](https://github.com/google/a2a). It discovers the vault through its
agent card and asks a natural-language question; the vault answers from its Flair
memory. This is the interop the vault advertises — another agent on your mesh
reading sovereign memory over a standard protocol, with no shared database and no
direct API coupling.

### Run it

Start the vault backend:

```bash
uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8000
```

Ask it something (store a memory or two first via the web UI or `/upload`):

```bash
uv run python examples/a2a_peer_query.py "how much did I spend at Joe's Grill?"
```

The vault routes the question to `search_memory` and replies over A2A; the script
prints the answer.

### Options

- `--url` — vault backend base URL (default `http://127.0.0.1:8000`).
- `--app-name` — ADK app name (default `app`).
- `--card-url` — full agent-card URL, overriding `--url`/`--app-name` (useful for
  a deployed agent).

Against a deployed agent, Google credentials are attached automatically when
`google.auth.default()` resolves them; a local backend needs no auth.

### What's covered by tests

The URL construction and reply-parsing helpers are unit-tested in
`tests/unit/test_a2a_peer_query.py`. The live send needs the backend running plus
Gemini and Flair, so it is exercised by running the command above, not in CI.
