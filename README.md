# Document Generator

**GenAI-Enabled Project Lifecycle Documentation Platform**  
Automatically generates BRDs, RFPs, SOWs, Proposals, Tech Specs, and Scope Documents from uploaded project files using Azure OpenAI / Google Gemini.

---

## Architecture Overview

```
document-generator/
├── Data_Ingestion/          # Flask backend (REST API)
│   ├── api/                 # run_server.py — 26+ REST endpoints
│   ├── parsers/             # PDF, DOCX, PPTX, Excel, Vision AI
│   ├── storage/             # Azure Blob / local filesystem
│   ├── generation/          # LLM doc generation, DB ORM, derive fields
│   ├── models/              # Pydantic schemas
│   ├── agent/               # Google ADK agent
│   └── requirements.txt
├── frontend/                # React / Next.js frontend (separate team)
├── Data_Ingestion/api-docs.html             # Full API reference (open in browser)
└── IntelliDraft_API.postman_collection.json # Postman collection (29 requests)
```

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11.x or 3.12.x | Backend runtime |
| pip | latest | Package manager |
| Node.js | 18+ | Frontend (optional) |

---

## Setup

### 1 — Clone the repository

```bash
git clone https://github.com/Adit-Paliwal/document-generator.git
cd document-generator
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

> First install takes ~3–5 minutes (downloads ~200 MB of packages).

### 5 — Configure environment variables

```bash
cp Data_Ingestion/.env.example Data_Ingestion/.env
```

Open `Data_Ingestion/.env` and fill in your values:

```env
# Choose your LLM provider:  azure_openai | gemini
MODEL_PROVIDER=azure_openai

# For Azure OpenAI
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

```bash
python Data_Ingestion/run_server.py
```

The API will be available at **`http://localhost:7071/api/`**

### Start the Google ADK web UI (optional)

```bash
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
1. POST /api/upload                         → upload source files
2. POST /api/extract-project-data           → AI populates form fields from document
3. POST /api/projects                       → create & save project
4. POST /api/projects/{id}/derive-fields    → AI derives 12 extended project fields
5. GET  /api/projects/{id}/data             → verify ingested + derived fields
6. POST /api/generate/project/{id}          → start document generation job
7. GET  /api/generate/{job_id}              → poll until status = "completed"
8. GET  /api/generate/{job_id}/export?format=docx  → download final document
```

Open **`Data_Ingestion/api-docs.html`** (double-click) for the full interactive API reference.  
Import **`IntelliDraft_API.postman_collection.json`** into Postman for ready-to-run requests.

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `azure_openai` | Active LLM: `azure_openai` \| `gemini` |
| `LOCAL_MODE` | `true` | `true` = save files locally; `false` = Azure Blob + Cosmos |
| `LOCAL_DB` | `true` | `true` = SQLite; `false` = PostgreSQL / Azure SQL via `DATABASE_URL` |
| `ASYNC_GENERATION` | `true` | `true` = background thread (poll for status); `false` = synchronous |
| `VISION_ENABLED` | `true` | `true` = AI describes extracted images |
| `AZURE_GPT5_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_GPT5_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint URL |
| `AZURE_GPT5_MODEL_DEPLOYMENT_ID` | — | Azure deployment name |
| `GOOGLE_API_KEY` | — | Google Gemini API key |
| `DATABASE_URL` | — | Production DB connection string (only when `LOCAL_DB=false`) |

---

## Local Storage Layout

When `LOCAL_MODE=true` and `LOCAL_DB=true`, everything is stored under:

```
Data_Ingestion/local_storage/
├── intellidraft.db          # SQLite database (projects, jobs, sections…)
└── documents/
    └── <doc_id>/
        ├── source/          # Original uploaded file
        ├── images/          # Extracted images
        ├── tables/          # Extracted tables (CSV)
        └── meta.json        # Parsed document metadata
```

> `local_storage/` is in `.gitignore` — never committed.

---

## Switching LLM Providers

Edit `Data_Ingestion/.env` and change `MODEL_PROVIDER`:

```env
# Use Gemini
MODEL_PROVIDER=gemini
GOOGLE_API_KEY=your-key-here

# Use Azure OpenAI
MODEL_PROVIDER=azure_openai
AZURE_GPT5_OPENAI_API_KEY=your-key-here
AZURE_GPT5_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_GPT5_MODEL_DEPLOYMENT_ID=your-deployment-name
```

No code changes needed — the provider switch is purely config-driven.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `litellm` install fails on Windows | Enable Long Paths (Step 2 above) |
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
| REST API (26+ endpoints) | ✅ Complete |
| Project + DerivedData ORM models | ✅ Complete |
| AI-driven field derivation (`derive-fields`) | ✅ Complete |
| Document generation (BRD, RFP, SOW, Proposal, TechSpec, Scope) | ✅ Complete |
| Google ADK agent | ✅ Complete |
| Frontend integration | 🔄 In progress |
| Production deployment | 🔄 Pending |
