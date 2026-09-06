# Reaching the vault from your agents

How to query the Visual Memory Vault from the agents you already use, without a
separate app. The vault is a **spoke**; **Flair is the hub**. Agents reach the
vault's memories through Flair's own MCP surface, not through an MCP server on
the vault.

## Architecture

```
iPhone Shortcut ──upload──▶ Vault proxy ──▶ Vault agent (Gemini) ──▶ Flair (hub)
                                                                        ▲
Claude / other MCP agents ──query via Flair's MCP hub────────────────────┘
```

- The vault (a Google ADK agent on GCP) extracts text from screenshots and
  writes memories into Flair, scoped to `app=visual-memory-vault`, `user=user`,
  under the `visual-memory-vault` agent identity.
- Flair holds those memories on the fabric and exposes them through its MCP
  surface. Any MCP-capable agent reads them there.
- The vault exposes **no** MCP server of its own; that responsibility lives in
  the hub, where it is reusable by every spoke and every agent.

## What works today (verified)

- **Recall from the live vault.** Querying Flair as the `visual-memory-vault`
  identity against the fabric returns the vault's real stored memories
  (verified: recent screenshots came back through `memory_search`/`list`).
  Requirements: `FLAIR_AGENT_ID=visual-memory-vault`, the vault keyfile
  (`~/.flair/keys/visual-memory-vault.key`), and `FLAIR_URL` pointed at the
  fabric that holds the data. The local Flair daemon returns 401 for this
  identity, so point at the fabric, not localhost.
- **Claude Code / Cursor on the laptop, now.** They speak Flair's stdio MCP
  package (`@tpsdev-ai/flair-mcp`), so they can already recall vault memories
  with no hosting. This queries the same fabric.
- **Phone apps.** Of Claude, Gemini, and Grok, only **Claude** can use a custom
  remote MCP connector. The Gemini consumer app and the Grok app do not support
  a user's own MCP endpoint. So the phone path is Claude, via a hosted connector
  (Phase 1 below).

## Phase 1 — enable the hub for the Claude phone app (config, no code)

Flair has a native remote OAuth MCP surface ("Model 2", `docs/notes/mcp-oauth-model2.md`
in the Flair repo): a `/mcp` JSON-RPC endpoint over Streamable HTTP, guarded by
`@harperfast/oauth`, serving nine curated tools including `memory_search`. It is
experimental and default-off behind `FLAIR_MCP_OAUTH`, and its allowed-connector
hosts already list `claude.ai`/`claude.com`. Enabling it is config on the fabric
that holds the vault's data.

1. Add the OAuth MCP block to that fabric's `config.yaml`:

   ```yaml
   '@harperfast/oauth':
     package: '@harperfast/oauth'
     mcp:
       enabled: ${FLAIR_MCP_OAUTH}
       issuer: https://<fabric-public-origin>   # literal, not composed
       accessTokenTtl: 900
       dynamicClientRegistration:
         enabled: false
       clientIdMetadataDocuments:
         allowedHosts:
           - claude.ai
           - claude.com
       signingKeyPem: ${FLAIR_MCP_SIGNING_KEY_PEM}
   ```

2. Set env and restart the fabric:

   ```bash
   FLAIR_MCP_OAUTH=true          # must be exactly "true"
   FLAIR_MCP_ISSUER=https://<fabric-public-origin>
   ```

   `flair mcp enable` writes that config shape; `flair mcp enable --help` is
   authoritative.

3. Add a custom connector in Claude at `https://<fabric-public-origin>/mcp`.
   It authenticates over OAuth via a client-ID metadata document (no dynamic
   registration). Connect once, ask the connector "who am I" so the `bootstrap`
   tool returns the resolved agent id, then bind that subject to the vault's
   identity so the phone sees the vault's memories:

   ```bash
   flair mcp enable --principal visual-memory-vault --idp-subject <sub-from-connect>
   ```

4. Verify from Claude on the phone: ask it to search your vault.

Guardrails: this surface is experimental and default-off; treat it as
production only after the Sherlock sign-off the Flair doc requires, and pin
`signingKeyPem` in a cluster.

Result: phone text recall over the vault's memories, no code.

## Phase 2 — images in the hub (a Flair feature, design-review gated)

Text recall does not need the image bytes. To let an agent *view* a screenshot,
the image must live in the hub too. Harper supports blobs via `createBlob`
(`record.data = createBlob(Buffer.from(b64, 'base64'), { type })`), but no Flair
resource uses it yet, so this is a first-of-kind capability and lands through
Flair's normal design review (a `RECORD_TYPES` entry is PR-reviewed at
"same trust level as mcp-tools").

Design:

- **`Asset` table** (new, in `schemas/memory.graphql`): `id`, `agentId`,
  `memoryId` link, `contentType`, `data: Blob`, `createdAt`. Paired with a
  `record-type-kit`-conformant, owner-scoped, identity-gated resource whose
  `post()` runs `createBlob`, and a `RECORD_TYPES.Asset` entry.
- **Serve it through the hub**: one added curated tool that returns the image as
  MCP image content, and an asset id plus an OAuth-guarded asset URL included in
  `memory_search` results.
- **Vault spoke change**: on screenshot store, also write the bytes to `Asset`
  and record the asset id in the memory metadata (keep GCS during transition).

Open questions for the Flair review, not decidable from the spoke:

- **Federation**: do `Asset` blobs replicate across fabric instances, or stay
  instance-local with the memory carrying a fetch reference? `Federation.ts`'s
  table list and the schema's `originatorInstanceId` fields are the touch points.
- **Security**: adding a tenth curated tool touches the "curated by construction"
  model; the asset URL must be OAuth-scoped the same way the tools are.

## Why the vault has no MCP server

An earlier branch added an MCP server to the vault itself (PR #13). The
hub-and-spoke model supersedes it: access belongs in Flair, where one hub serves
every spoke and every agent, so #13 is retired in favor of Phase 1 + Phase 2.
