#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy_agent_engine.sh — Deploy IntelliDraft agents to Vertex AI Agent Engine
# ══════════════════════════════════════════════════════════════════════════════
#
# Uses the ADK CLI (adk deploy agent_engine) — no Python SDK needed.
#
# Prerequisites (must be done first):
#   1. bash gcp_setup.sh                        → creates .env.deploy + enables APIs
#   2. gcloud auth login                        → authenticate with GCP account
#   3. gcloud auth application-default login    → set Application Default Credentials (ADC)
#   4. source .env.deploy                       → loads GCP_PROJECT_ID, GCP_REGION, GCS_STAGING_BUCKET
#   5. pip install --upgrade google-adk          → install/upgrade to latest version
#
# NOTE: GEMINI_API_KEY is NOT required — ADC (Application Default Credentials) is used instead.
#       The agent container on GCP also uses ADC via the GOOGLE_CLOUD_PROJECT env var.
#       If you DO have a GEMINI_API_KEY, you can optionally export it and it will be used.
#
# HOW TO RUN (from the Intellidraft/ project root):
#   gcloud auth login
#   gcloud auth application-default login
#   source .env.deploy
#   bash deploy_agent_engine.sh
#
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Load config from gcp_setup.sh output ─────────────────────────────────────
if [[ -f ".env.deploy" ]]; then
  # shellcheck disable=SC1091
  source .env.deploy
fi

# ── Validate required vars ────────────────────────────────────────────────────
MISSING=()
[[ -z "${GCP_PROJECT_ID:-}"      ]] && MISSING+=("GCP_PROJECT_ID")
[[ -z "${GCP_REGION:-}"          ]] && MISSING+=("GCP_REGION")
[[ -z "${GCS_STAGING_BUCKET:-}"  ]] && MISSING+=("GCS_STAGING_BUCKET")
# NOTE: GEMINI_API_KEY is optional — ADC is used if not set

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo ""
  echo "✗  Missing required variables: ${MISSING[*]}"
  echo ""
  echo "   Fix:"
  echo "     source .env.deploy   # loads GCP_PROJECT_ID, GCP_REGION, GCS_STAGING_BUCKET"
  echo ""
  echo "   Also ensure you are authenticated:"
  echo "     gcloud auth login"
  echo "     gcloud auth application-default login"
  echo ""
  exit 1
fi

# ── Check ADC is active ───────────────────────────────────────────────────────
if ! gcloud auth application-default print-access-token &>/dev/null; then
  echo ""
  echo "✗  Application Default Credentials (ADC) not set."
  echo "   Run these two commands, then re-run this script:"
  echo ""
  echo "     gcloud auth login"
  echo "     gcloud auth application-default login"
  echo ""
  exit 1
fi

# ── Check adk is installed ────────────────────────────────────────────────────
if ! command -v adk &>/dev/null; then
  echo ""
  echo "✗  'adk' command not found."
  echo "   Install with:  pip install --upgrade google-adk"
  echo "   Then re-run this script."
  echo ""
  exit 1
fi

# ── Must run from Intellidraft/ — the PARENT of Data_Ingestion/ ──────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d "Data_Ingestion" ]]; then
  echo "✗  Data_Ingestion/ folder not found."
  echo "   Run this script from the Intellidraft/ project root."
  exit 1
fi

# ── Deploy ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   IntelliDraft — Agent Engine Deploy (Step 3/4)     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo "  Project : ${GCP_PROJECT_ID}"
echo "  Region  : ${GCP_REGION}"
echo "  Bucket  : gs://${GCS_STAGING_BUCKET}"
echo ""
echo "  This takes 8-12 minutes — GCP is building and packaging"
echo "  the agent container. The terminal will show progress."
echo ""

# ── Locate env.agent_engine (runtime env vars for the container) ──────────────
# NOTE: --set_env_vars is NOT supported in the current ADK CLI.
#       The correct flag is --env_file pointing to a .env file.
#       GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are RESERVED by GCP —
#       never put them in this file or the deployment will fail.
ENV_FILE="${SCRIPT_DIR}/env.agent_engine"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "✗  env.agent_engine file not found at: ${ENV_FILE}"
  echo "   This file should have been included in the deployment zip."
  exit 1
fi
echo "  Env   : Using ${ENV_FILE}"
echo "  Auth  : Application Default Credentials (ADC)"
echo ""

adk deploy agent_engine \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --staging_bucket="gs://${GCS_STAGING_BUCKET}" \
  --display_name="IntelliDraft Document Generator" \
  --description="IntelliDraft multi-agent system: document parsing, context loading, and generation with chat-based section modification." \
  --env_file="${ENV_FILE}" \
  Data_Ingestion

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅  Agent Engine deployment complete!              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  ── What to do next ───────────────────────────────────"
echo ""
echo "  1. Find your Agent Engine Resource ID:"
echo "     https://console.cloud.google.com/vertex-ai/agents/"
echo "     → Click on 'IntelliDraft Document Generator'"
echo "     → Copy the numeric ID from the URL"
echo ""
echo "  2. Run the sanity test (Step 4/4):"
echo "     python sanity_test.py \\"
echo "         --api-url=\$CLOUD_RUN_URL \\"
echo "         --project=${GCP_PROJECT_ID} \\"
echo "         --region=${GCP_REGION} \\"
echo "         --agent-id=<numeric-id-from-step-1>"
echo ""
