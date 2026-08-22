#!/usr/bin/env bash
# ==============================================================================
# Visual Memory Vault - Automated Agent Onboarding for Harper Fabric / Flair
# ==============================================================================
set -euo pipefail

AGENT_ID="${1:-visual-memory-vault}"
FLAIR_TARGET="${2:-https://casa.heskew.harperfabric.com}"
GCP_PROJECT="${3:-visual-memory-vault-506303}"

echo "======================================================================"
echo "🤖 Onboarding Agent Identity: ${AGENT_ID}"
echo "📍 Target Flair Host:        ${FLAIR_TARGET}"
echo "☁️  GCP Project:              ${GCP_PROJECT}"
echo "======================================================================"

# 1. Prompt for admin password if not provided in environment
if [ -z "${FLAIR_ADMIN_PASS:-}" ]; then
  read -rsp "Enter Harper Fabric Admin Password for ${FLAIR_TARGET}: " FLAIR_ADMIN_PASS
  echo ""
fi

# 2. Register agent on Harper Fabric / Flair
echo "🔑 Generating Ed25519 keypair and registering agent on Flair..."
flair agent add "${AGENT_ID}" \
  --target "${FLAIR_TARGET}" \
  --admin-pass "${FLAIR_ADMIN_PASS}"

KEY_PATH="${HOME}/.flair/keys/${AGENT_ID}.key"
if [ ! -f "${KEY_PATH}" ]; then
  echo "❌ Error: Expected key file not found at ${KEY_PATH}"
  exit 1
fi

# 3. Encode private key seed to Base64 for container deployment
KEY_B64=$(base64 < "${KEY_PATH}" | tr -d '\n')

echo "✅ Agent successfully registered and key saved to ${KEY_PATH}"

# 4. Optional: Update Google Cloud Agent Runtime deployment
read -rp "Do you want to deploy these credentials to Google Cloud Agent Runtime now? [y/N]: " CONFIRM_DEPLOY
if [[ "${CONFIRM_DEPLOY}" =~ ^[Yy]$ ]]; then
  echo "🚀 Updating Google Cloud Agent Runtime deployment..."
  agents-cli deploy \
    --project "${GCP_PROJECT}" \
    --update-env-vars "FLAIR_URL=${FLAIR_TARGET},FLAIR_AGENT_ID=${AGENT_ID},FLAIR_PRIVATE_KEY_B64=${KEY_B64}"
  echo "🎉 Agent Runtime successfully updated with sovereign credentials!"
else
  echo ""
  echo "To update manually later, run:"
  echo "agents-cli deploy --project ${GCP_PROJECT} --update-env-vars \"FLAIR_URL=${FLAIR_TARGET},FLAIR_AGENT_ID=${AGENT_ID},FLAIR_PRIVATE_KEY_B64=${KEY_B64}\""
fi
