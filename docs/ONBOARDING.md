# Agent Onboarding & Sovereign Authentication Guide

This document explains how to onboard, provision, and deploy autonomous agents with sovereign **Ed25519 cryptographic authentication** to **Harper Fabric** and **Flair**.

---

## 🔐 Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│  Google Cloud Agent Runtime (Deployed ADK Agent)       │
│  - Environment: FLAIR_URL, FLAIR_AGENT_ID,             │
│                 FLAIR_PRIVATE_KEY_B64                  │
└───────────────────────────┬────────────────────────────┘
                            │
                            │ HTTPS / Signed REST
                            │ Header: TPS-Ed25519 <id>:<ts>:<nonce>:<sig>
                            ▼
┌────────────────────────────────────────────────────────┐
│  Harper Fabric / Flair Host (casa.heskew.harperfabric) │
│  - Validates signature against registered public key   │
│  - Persists memory & updates HNSW vector index         │
└────────────────────────────────────────────────────────┘
```

Every agent possesses a sovereign identity defined by:
1. **`agentId`**: Unique principal name (e.g. `visual-memory-vault`).
2. **Ed25519 Keypair**: 
   - Public Key (`.pub`): Stored in Harper Fabric (`/Agent` table).
   - Private Key (`.key` / Base64 seed): Kept securely with the agent to sign all memory transactions.

---

## 🚀 Quick Automated Onboarding (1 Step)

Run the included onboarding script:

```bash
./scripts/onboard_agent.sh [AGENT_ID] [FLAIR_TARGET] [GCP_PROJECT]
```

Example:
```bash
./scripts/onboard_agent.sh visual-memory-vault https://casa.heskew.harperfabric.com visual-memory-vault-506303
```

This script will:
1. Generate an Ed25519 keypair for the agent.
2. Authenticate to Harper Fabric with the admin password and register the public key.
3. Extract the Base64-encoded private key seed.
4. Optionally update Google Cloud Agent Runtime via `agents-cli deploy --update-env-vars`.

---

## 🛠️ Manual Step-by-Step Onboarding

If you prefer to perform each step manually:

### Step 1: Provision the Agent Identity on Harper Fabric

Using the `flair` CLI:

```bash
flair agent add visual-memory-vault \
  --target https://casa.heskew.harperfabric.com \
  --admin-pass "<YOUR_HARPER_ADMIN_PASSWORD>"
```

* This generates `~/.flair/keys/visual-memory-vault.key` (private) and `~/.flair/keys/visual-memory-vault.pub` (public).
* It registers the public key with the Harper Fabric `/Agent` registry.

---

### Step 2: Encode the Private Key for Cloud Deployments

Container runtimes (like Vertex AI Agent Runtime or Cloud Run) do not mount local home directories. Encode the 32-byte binary key to a Base64 string:

```bash
base64 < ~/.flair/keys/visual-memory-vault.key | tr -d '\n'
```

---

### Step 3: Deploy Credentials to Google Cloud Agent Runtime

Update the deployed agent environment variables using `agents-cli`:

```bash
agents-cli deploy \
  --project visual-memory-vault-506303 \
  --update-env-vars "FLAIR_URL=https://casa.heskew.harperfabric.com,FLAIR_AGENT_ID=visual-memory-vault,FLAIR_PRIVATE_KEY_B64=<YOUR_B64_KEY>"
```

---

### Step 4: Verify Live End-to-End Recall

1. Send an image upload from your phone via the Cloud Run proxy (send-and-forget; expect `202 Accepted` with `image_path`, not an agent writeup):
   ```bash
   curl -X POST https://visual-memory-vault-proxy-151358874679.us-east1.run.app/upload \
     -H "X-Api-Key: <YOUR_PROXY_KEY>" \
     -F "file=@receipt.jpg" \
     -F "subject=Test Onboarding"
   ```
   Cloud Run can freeze CPU after `202` and scale the instance to zero, so a request-scoped `create_task` may never finish. The proxy writes a durable ingest job next to the image (local `MEDIA_DIR`, and the existing GCS bucket when `GCS_BUCKET_NAME` is set). A later request (`/chat`, `/upload`, `/health`) or internal `POST /ingest` drains pending jobs. Shortcut clients must not call `/ingest`.

   2. Verify the memory appears in your `casa.heskew` Flair instance:
   ```bash
   flair memory list --target https://casa.heskew.harperfabric.com
   ```
