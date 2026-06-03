# IntelliDraft

**GenAI-Enabled Project Lifecycle Documentation Platform** for AESL.  
Automatically generates BRDs, RFPs, SOWs, Proposals, Tech Specs, and Scope Documents from uploaded project files using Azure GPT-5 / Google Gemini.

---

## Architecture Overview

```
IntelliDraft/
├── Data_Ingestion/          # Azure Functions backend (REST API)
│   ├── api/                 # function_app.py  — 26 REST endpoints
│   ├── parsers/             # PDF, DOCX, PPTX, Excel, Vision AI
│   ├── storage/             # Azure Blob / local filesystem
│   ├── generation/          # LLM doc generation, DB ORM, derive fields
│   ├── models/              # Pydantic schemas
│   ├── agent/               # Google ADK agent
│   └── requirements.txt
├── frontend/                # React / Next.js frontend (separate team)
├── IntelliDraft_API_Docs.html      # Full API reference (open in browser)
└── IntelliDraft.postman_collection.json  # Postman collection
```

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11.x or 3.12.x | Backend runtime (3.13+ not yet supported — azure-functions 1.x required) |
| pip | latest | Package manager |
| Azure Functions Core Tools | v4 | Run Functions locally |
| Node.js | 18+ | Frontend (optional) |

### Install Azure Functions Core Tools (v4)

```powershell
# Windows (winget)
winget install Microsoft.AzureFunctionsCoreTools

# macOS
brew tap azure/functions
brew install azure-functions-core-tools@4

# Ubuntu / Debian
curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg
echo "deb [arch=amd64] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/azure-cli.list
apt-get update && apt-get install -y azure-functions-core-tools-4
```

---

## Setup

### 1 — Clone the repository

```bash
git clone https://github.com/Adit-Paliwal/intellidraft.git
cd intellidraft
```

### 2 — Enable Windows Long Paths (Windows only — required for litellm)

Run **PowerShell as Administrator**:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

Restart your terminal after running this command.

> **Skip this step on Linux / macOS** — Long Path limits don't apply.

### 3 — Create and activate a virtual environment

```bash
# Create
python -m venv env

# Activate — Windows (PowerShell)
env\Scripts\Activate.ps1

# Activate — Windows (CMD)
env\Scripts\activate.bat

# Activate — Linux / macOS
source env/bin/activate
```

### 4 — Install dependencies

```bash
pip install -r Data_Ingestion/requirements.txt
```

> This installs all packages with exact pinned versions for reproducibility.  
> First install takes ~3–5 minutes (downloads ~200 MB of packages).

### 5 — Configure environment variables

```bash
# Copy the template
cp Data_Ingestion/.env.example Data_Ingestion/.env
```

Open `Data_Ingestion/.env` in any text editor and fill in your values:

```env
# Choose your LLM provider:  azure_gpt5 | gemini | azure_openai
MODEL_PROVIDER=azure_gpt5

# For Azure GPT-5 (default)
AZURE_GPT5_OPENAI_API_KEY=<your-azure-api-key>
AZURE_GPT5_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_GPT5_MODEL_DEPLOYMENT_ID=<your-deployment-name>

# For Gemini (alternative)
GOOGLE_API_KEY=<your-google-api-key>
```

> **Keep `LOCAL_MODE=true` and `LOCAL_DB=true`** for local development —  
> no Azure Blob, Cosmos DB, or SQL Server needed.

---

## Running Locally

### Start the Azure Functions backend

```bash
cd Data_Ingestion
func start
```

The API will be available at **`http://localhost:7071/api/`**

Expected output:
```
Azure Functions Core Tools
Core Tools Version:  4.x.x
...
Functions:
  upload_document: [POST] http://localhost:7071/api/documents
  list_documents:  [GET]  http://localhost:7071/api/documents
  ...
```

### Start the Google ADK web UI (optional)

In a **separate terminal** from the repo root:

```bash
# Activate the same venv first
source env/bin/activate   # or env\Scripts\Activate.ps1 on Windows

adk web
```

ADK web UI available at **`http://localhost:8000`**

---

## API Quick Reference

### Base URL (local)
```
http://localhost:7071/api
```

### Typical usage flow

```
1. POST /api/documents              → upload source files
2. POST /api/projects               → create project (attach document_ids)
3. POST /api/projects/{id}/derive-fields  → AI derives 12 project fields
4. GET  /api/projects/{id}/derived-data   → verify derived fields
5. POST /api/generate               → start document generation job
6. GET  /api/jobs/{job_id}/status   → poll until status = "completed"
7. GET  /api/jobs/{job_id}/export?format=docx  → download final document
```

Open **`IntelliDraft_API_Docs.html`** (double-click) for the full interactive API reference.  
Import **`IntelliDraft.postman_collection.json`** into Postman for ready-to-run requests.

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `azure_gpt5` | Active LLM: `azure_gpt5` \| `gemini` \| `azure_openai` |
| `LOCAL_MODE` | `true` | `true` = save files locally; `false` = Azure Blob + Cosmos |
| `LOCAL_DB` | `true` | `true` = SQLite; `false` = Azure SQL / PostgreSQL via `DATABASE_URL` |
| `ASYNC_GENERATION` | `true` | `true` = background thread (poll for status); `false` = synchronous |
| `VISION_ENABLED` | `true` | `true` = AI describes extracted images |
| `AZURE_GPT5_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_GPT5_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint URL |
| `AZURE_GPT5_MODEL_DEPLOYMENT_ID` | `project-pulse-gpt-5` | Azure deployment name |
| `GOOGLE_API_KEY` | — | Google Gemini API key |
| `DATABASE_URL` | — | Production DB connection string (only when `LOCAL_DB=false`) |

---

## Local Storage Layout

When `LOCAL_MODE=true` and `LOCAL_DB=true`, everything is stored under:

```
Data_Ingestion/local_storage/
├── intellidraft.db          # SQLite database (projects, jobs, sections…)
└── <doc_id>/
    ├── raw/                 # Original uploaded file
    └── parsed/              # Extracted text, images, tables (JSON)
```

> `local_storage/` is in `.gitignore` — never committed.

---

## Switching LLM Providers

Edit `Data_Ingestion/.env` and change `MODEL_PROVIDER`:

```env
# Use Gemini
MODEL_PROVIDER=gemini
GOOGLE_API_KEY=your-key-here

# Use Azure OpenAI (other deployment)
MODEL_PROVIDER=azure_openai
AZURE_OPENAI_API_KEY=your-key-here
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```

No code changes needed — the provider switch is purely config-driven.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `litellm` install fails on Windows | Enable Long Paths (Step 2 above) |
| `func: command not found` | Install Azure Functions Core Tools v4 |
| `ModuleNotFoundError` on startup | Check venv is activated; re-run `pip install -r requirements.txt` |
| `CORS error` from frontend | Backend is running; check `http://localhost:7071/api/health` |
| LLM returns 502 | API key or endpoint misconfigured in `.env`; check `MODEL_PROVIDER` |
| SQLite `OperationalError` | Delete `Data_Ingestion/local_storage/intellidraft.db` and restart |

---

## Project Status

| Module | Status |
|--------|--------|
| Document parsers (PDF, DOCX, PPTX, Excel) | ✅ Complete |
| Vision AI image analysis | ✅ Complete |
| Azure Functions API (26 endpoints) | ✅ Complete |
| Project + DerivedData ORM models | ✅ Complete |
| AI-driven field derivation (`derive-fields`) | ✅ Complete |
| Document generation (BRD, RFP, SOW…) | ✅ Complete |
| Google ADK agent | ✅ Complete |
| Frontend integration | 🔄 In progress |
| Production Azure deployment | 🔄 Pending |
