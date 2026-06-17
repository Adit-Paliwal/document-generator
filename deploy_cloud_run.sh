#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy_cloud_run.sh — Deploy IntelliDraft Flask API to Google Cloud Run
# ══════════════════════════════════════════════════════════════════════════════
#
# What this does:
#   1. Builds the Docker image and pushes it to Artifact Registry (or GCR)
#   2. Deploys / updates the Cloud Run service
#   3. Prints the service URL when done
#
# Prerequisites (run once before first deploy):
#   gcloud auth login
#   gcloud auth configure-docker
#   gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
#
# Usage:
#   export GCP_PROJECT_ID=your-project-id
#   export GEMINI_API_KEY=AIzaXXXXXXXXXXXXXX      # from aistudio.google.com/app/apikey
#   bash deploy_cloud_run.sh
#
# Optional overrides (set before running):
#   GCP_REGION          default: us-central1
#   CR_SERVICE_NAME     default: intellidraft-api
#   CR_MEMORY           default: 2Gi
#   CR_CPU              default: 2
#   CR_MAX_INSTANCES    default: 3
#   CR_TIMEOUT          default: 300
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Required ──────────────────────────────────────────────────────────────────
: "${GCP_PROJECT_ID:?  ✗  GCP_PROJECT_ID is not set. Run:  export GCP_PROJECT_ID=your-project-id}"
: "${GEMINI_API_KEY:?  ✗  GEMINI_API_KEY is not set.   Run:  export GEMINI_API_KEY=AIzaXXXXX}"

# ── Defaults ──────────────────────────────────────────────────────────────────
GCP_REGION="${GCP_REGION:-asia-south1}"
CR_SERVICE_NAME="${CR_SERVICE_NAME:-intellidraft-api}"
CR_MEMORY="${CR_MEMORY:-2Gi}"
CR_CPU="${CR_CPU:-2}"
CR_MAX_INSTANCES="${CR_MAX_INSTANCES:-3}"
CR_TIMEOUT="${CR_TIMEOUT:-300}"

# Image in Artifact Registry  (gcr.io also works if you prefer the old registry)
IMAGE="gcr.io/${GCP_PROJECT_ID}/${CR_SERVICE_NAME}:latest"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   IntelliDraft — Cloud Run Deployment                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo "  Project : ${GCP_PROJECT_ID}"
echo "  Region  : ${GCP_REGION}"
echo "  Service : ${CR_SERVICE_NAME}"
echo "  Image   : ${IMAGE}"
echo ""

# ── Step 1: Build + push image ────────────────────────────────────────────────
echo "▶ Step 1/2 — Building and pushing Docker image …"
echo "  (this takes ~3-5 min on first build, faster on rebuilds)"
echo ""

# Cloud Build is used so the build runs in GCP — no large Docker upload needed.
# The Dockerfile lives in the repo root.
gcloud builds submit \
    --tag "${IMAGE}" \
    --project "${GCP_PROJECT_ID}" \
    .

echo ""
echo "  ✓ Image pushed: ${IMAGE}"
echo ""

# ── Step 2: Deploy to Cloud Run ───────────────────────────────────────────────
echo "▶ Step 2/2 — Deploying to Cloud Run …"
echo ""

gcloud run deploy "${CR_SERVICE_NAME}" \
    --image        "${IMAGE}" \
    --platform     managed \
    --region       "${GCP_REGION}" \
    --project      "${GCP_PROJECT_ID}" \
    --memory       "${CR_MEMORY}" \
    --cpu          "${CR_CPU}" \
    --timeout      "${CR_TIMEOUT}" \
    --max-instances "${CR_MAX_INSTANCES}" \
    --allow-unauthenticated \
    --set-env-vars "\
MODEL_PROVIDER=gemini,\
GEMINI_API_KEY=${GEMINI_API_KEY},\
LOCAL_MODE=true,\
LOCAL_DB=true,\
INTELLIDRAFT_DB_DIR=/tmp/intellidraft,\
ASYNC_GENERATION=true,\
VISION_ENABLED=true,\
PYTHONUTF8=1,\
GOOGLE_CLOUD_PROJECT=${GCP_PROJECT_ID},\
VERTEX_LOCATION=${GCP_REGION}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
SERVICE_URL=$(gcloud run services describe "${CR_SERVICE_NAME}" \
    --region "${GCP_REGION}" --project "${GCP_PROJECT_ID}" \
    --format "value(status.url)")

echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅  Cloud Run deployment complete!                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  API URL    : ${SERVICE_URL}/api"
echo "  Health     : ${SERVICE_URL}/api/health"
echo ""
echo "  ⚠️  NOTE: LOCAL_DB=true means SQLite is in /tmp — data is"
echo "      ephemeral and lost on container restart."
echo "      For production: switch to Cloud SQL (set LOCAL_DB=false"
echo "      and DATABASE_URL=postgresql://... in Cloud Run env vars)."
echo ""
echo "  Next step: deploy ADK agents → run deploy_agent_engine.py"
echo "  and set FLASK_API_URL=${SERVICE_URL} so the frontend"
echo "  points to this Cloud Run URL instead of localhost:7071."
echo ""
