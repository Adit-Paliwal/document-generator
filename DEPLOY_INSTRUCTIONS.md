# IntelliDraft — Agent Engine Deployment Guide
### Complete step-by-step commands for deploying to Vertex AI Agent Engine

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | **3.11** exactly | Container is Python 3.11; must match |
| gcloud CLI | latest | https://cloud.google.com/sdk/docs/install |
| GCP access | — | Needs Vertex AI + Storage permissions on the project |
| GCS bucket | existing | A staging bucket must already exist in the project |

---

## Step 0 — Install required tools

Run these once on the deployment machine:

```bash
# Install / upgrade the ADK CLI (handles the actual deploy)
pip install --upgrade google-adk

# Install agent dependencies (needed locally to package the agent)
pip install -r Data_Ingestion/requirements.txt

# Verify ADK is available
adk --version
```

---

## Step 1 — Get the code

```bash
# Option A — Clone the repo (recommended)
git clone https://github.com/<org>/Intellidraft.git
cd Intellidraft

# Option B — Download the ZIP from GitHub and extract it
# Then cd into the extracted folder:
cd Intellidraft
```

**CRITICAL — All subsequent commands must be run from `Intellidraft/`**
(the parent of `Data_Ingestion/`, NOT from inside `Data_Ingestion/`)

Verify you are in the right directory:
```bash
ls Data_Ingestion/      # Should list: agents/, api/, generation/, parsers/, etc.
```

---

## Step 2 — Configure GCP project

Open `gcp_setup.sh` and fill in the two values at the top:

```bash
GCP_PROJECT_ID="ailabs-energy-trading-dev"   # ← your GCP project ID
GCS_STAGING_BUCKET="your-existing-bucket"    # ← bucket name WITHOUT gs://
```

Then run it:

```bash
bash gcp_setup.sh
```

This enables the required APIs and writes a `.env.deploy` file with your config.

---

## Step 3 — Authenticate with GCP

```bash
# Log in with your Google account
gcloud auth login

# Set Application Default Credentials (ADC) — used by Vertex AI SDK
gcloud auth application-default login

# Verify which account is active
gcloud config get-value account
gcloud config get-value project
```

> **Note:** No `GEMINI_API_KEY` is needed. The agent uses ADC (Application Default Credentials)
> to call Gemini via Vertex AI. The container also uses ADC at runtime.

---

## Step 4 — Load config and deploy

```bash
# Load project config written by gcp_setup.sh
source .env.deploy

# Verify the variables loaded correctly
echo "Project : $GCP_PROJECT_ID"
echo "Region  : $GCP_REGION"
echo "Bucket  : $GCS_STAGING_BUCKET"

# Deploy to Agent Engine
# (MUST run from Intellidraft/ — the script checks this and exits if wrong)
bash deploy_agent_engine.sh
```

**Expected output:**
```
╔══════════════════════════════════════════════════════╗
║   IntelliDraft — Agent Engine Deploy (Step 3/4)     ║
╚══════════════════════════════════════════════════════╝
  Project : ailabs-energy-trading-dev
  Region  : asia-south1
  Bucket  : gs://your-bucket
  ...
  This takes 8-12 minutes — GCP is building and packaging
  the agent container. The terminal will show progress.
```

When it finishes, the output shows:
```
╔══════════════════════════════════════════════════════╗
║   ✅  Agent Engine deployment complete!              ║
╚══════════════════════════════════════════════════════╝
```

**Save the Agent Engine Resource ID** from the GCP console or the deploy output.
You will need it for the sanity test.

---

## Step 5 — Get the Resource ID

1. Open: https://console.cloud.google.com/vertex-ai/agents/
2. Select your project (`ailabs-energy-trading-dev`) and region (`asia-south1`)
3. Click on **"IntelliDraft Document Generator"**
4. Copy the numeric ID from the URL — it looks like `123456789012345`

---

## Step 6 — Verify the deployment

```bash
# Install the test dependency
pip install requests

# Run sanity test (skip Cloud Run — only testing Agent Engine here)
python sanity_test.py \
    --project=$GCP_PROJECT_ID \
    --region=$GCP_REGION \
    --agent-id=<resource-id-from-step-5> \
    --skip-cloud-run
```

**Expected output:**
```
╔══════════════════════════════════════════════════════╗
║   IntelliDraft — Deployment Sanity Test              ║
╚══════════════════════════════════════════════════════╝
  ✓  Test 5: Agent Engine: create session
       session_id=abc123...
  ✓  Test 6: Agent Engine: chat response
       Response: Hello! I'm IntelliDraft, an AI assistant for generating ...

──────────────────────────────────────────────────────
  Results: 2/2 passed
──────────────────────────────────────────────────────

  🎉  All tests passed — IntelliDraft is live!
```

---

## Quick re-deploy (after code changes)

```bash
# Pull latest code
git pull origin main

# Re-run deploy (same commands as above, no gcp_setup.sh needed again)
source .env.deploy
bash deploy_agent_engine.sh
```

To update an existing deployment instead of creating a new one, use the Python script:

```bash
python deploy_agent_engine.py \
    --project=$GCP_PROJECT_ID \
    --region=$GCP_REGION \
    --bucket=$GCS_STAGING_BUCKET \
    --update \
    --resource-id=<resource-id-from-previous-deploy>
```

---

## Common errors and fixes

### Error: `Data_Ingestion/ folder not found`
```
✗  Data_Ingestion/ folder not found in /home/user/Data_Ingestion
   This script MUST be run from the Intellidraft/ project root.
```
**Fix:** You are inside the wrong directory. Run:
```bash
cd ..
bash deploy_agent_engine.sh
```

### Error: `No module named 'agents'` on container
**Cause:** Script was run from inside `Data_Ingestion/` or only the `agents/` subdirectory was packaged.  
**Fix:** Run from `Intellidraft/` (the parent). The script now guards against this.

### Error: `No module named 'Data_Ingestion'` on container
**Cause:** Old deploy script used wrong import path.  
**Fix:** This is already fixed in the current version. Pull latest code and redeploy.

### Error: `Application Default Credentials not set`
```
✗  Application Default Credentials (ADC) not set.
```
**Fix:**
```bash
gcloud auth application-default login
```

### Error: `adk: command not found`
**Fix:**
```bash
pip install --upgrade google-adk
```

### Error: `Missing required variables: GCP_PROJECT_ID`
**Fix:**
```bash
source .env.deploy
```

### Deployment takes too long / times out
The first deploy takes 8–12 minutes (GCP builds the container image). This is normal.
Do not cancel — wait for it to complete.

---

## Files in this package

| File | Purpose |
|---|---|
| `DEPLOY_INSTRUCTIONS.md` | This file |
| `gcp_setup.sh` | Step 2: one-time GCP project setup |
| `env.agent_engine` | Container runtime environment variables |
| `deploy_agent_engine.sh` | Step 4: deploy via ADK CLI (recommended) |
| `deploy_agent_engine.py` | Step 4 alt: deploy via Python SDK (for updates) |
| `sanity_test.py` | Step 6: verify the deployed agent responds correctly |

---

## Architecture note

The deployment packages the entire `Data_Ingestion/` directory as one unit:

```
Data_Ingestion/
├── __init__.py           ← exposes root_agent for ADK discovery
├── agents/
│   ├── orchestrator.py   ← IntelliDraftOrchestrator (routes requests)
│   ├── doc_parser/       ← DocParserAgent
│   ├── context_collector/← ContextCollectorAgent
│   └── document_generator/← DocumentGeneratorAgent
├── generation/           ← document generation (BRD, RFP, SOW, etc.)
├── parsers/              ← PDF, DOCX, PPTX, Excel parsers
├── storage/              ← file storage layer
├── api/                  ← REST API (Cloud Run only — not used on Agent Engine)
└── requirements.txt      ← all Python dependencies
```

The container runs in `asia-south1` with Python 3.11 and uses Application Default Credentials to call Gemini 2.5 Flash via Vertex AI.
