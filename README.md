# Document Generator

**GenAI-Enabled Project Lifecycle Documentation Platform**  
Automatically generates BRDs, RFPs, SOWs, Proposals, Tech Specs, and Scope Documents from uploaded project files using Azure OpenAI / Google Gemini.

---

## Architecture Overview

```
Intellidraft/
├── Data_Ingestion/                 # FastAPI backend (REST API — port 7071)
│   ├── main.py                     # 60+ REST endpoints (Swagger at /docs)
│   ├── app.yaml                    # Databricks Apps deployment config
│   ├── ontology/                   # Business ontology pack (prompt grounding)
│   ├── parsers/                    # PDF, DOCX, PPTX, Excel, Vision AI
│   ├── storage/                    # Databricks Volumes / local filesystem
│   ├── generation/                 # LLM generation, review, validation agent, DB ORM
│   ├── models/                     # Pydantic schemas
│   ├── tests/                      # pytest suites + API contract + load test
│   ├── agents/                     # Google ADK multi-agent system
│   │   ├── orchestrator.py         # Root LlmAgent — routes to sub-agents
│   │   ├── doc_parser/             # Agent 1 — upload, parse, Vision AI
│   │   ├── context_collector/      # Agent 2 — load project context from DB
│   │   ├── document_generator/     # Agent 3 — generate, modify, export
│   │   └── reviewer/               # Agent 4 — share, comments, AI reviews
│   └── requirements.txt
├── frontend-react/                 # React SPA (Vite + Tailwind) — primary UI
├── Data_Ingestion/frontend/        # Legacy single-file HTML pages (fallback)
└── DATABRICKS_DEPLOY_GUIDE.md      # The only supported deployment target
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

**Gemini (recommended — free API key from Google AI Studio):**
```env
MODEL_PROVIDER=gemini
GEMINI_API_KEY=<your-gemini-api-key>   # get at https://aistudio.google.com/app/apikey
```

**Azure GPT-5 (fallback / enterprise):**
```env
MODEL_PROVIDER=azure_gpt5
AZURE_GPT5_OPENAI_API_KEY=<your-azure-api-key>
AZURE_GPT5_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_GPT5_MODEL_DEPLOYMENT_ID=<your-deployment-name>
```

> **Keep `LOCAL_MODE=true` and `LOCAL_DB=true`** for local development —  
> no Azure Blob, Cosmos DB, or SQL Server needed.

> **Gemini is the primary LLM.** Azure GPT-5 is the automatic fallback — used only  
> when Gemini credentials are absent or a Gemini call fails. No code changes needed  
> to switch providers; just update `MODEL_PROVIDER` in `.env`.

---

## Setup & Deployment

- **Local setup on any machine** (native or Docker): see **[SETUP.md](SETUP.md)**
- **Production on Azure Databricks**: see **[DATABRICKS_DEPLOY_GUIDE.md](DATABRICKS_DEPLOY_GUIDE.md)**
- **Docker / self-host**: `docker compose up -d --build` → http://localhost:7071/

Quick native run below.

---

## Running Locally

### 1 — Start the FastAPI server

```bash
python Data_Ingestion/main.py
```

API available at **`http://localhost:7071/api/`**  
Health check: `http://localhost:7071/api/health` · Swagger: `http://localhost:7071/docs`

### 2 — Open the Frontend

The React SPA is served by the API itself at **`http://localhost:7071/`**
(build it once with `cd frontend-react && npm install && npm run build`).
For frontend development with hot reload: `cd frontend-react && npm run dev` → http://localhost:5173.

### 3 — Start the Google ADK web UI (optional — AI agent chat)

```bash
# Run from the Intellidraft/ parent directory (not Data_Ingestion/)
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
1. POST /api/upload                              → upload source file(s)
2. GET  /api/document/{doc_id}                   → verify parse summary (text, images, tables)
3. POST /api/extract-project-data                → AI populates form fields from document
3b. POST /api/projects/draft                     → persist extracted fields immediately (no validation)
4. PATCH /api/projects/{id}                      → fill in remaining fields, set document_type
   — OR —
   POST /api/projects                            → create project in one shot (all required fields)
5. GET  /api/projects/{id}                       → verify project (document_type, status, doc IDs)
6. GET  /api/projects/{id}/data                  → verify ingested + derived fields
7. POST /api/generate/project/{id}               → start document generation job
8. GET  /api/generate/{job_id}                   → poll until status = "completed"
9. GET  /api/generate/{job_id}/export?format=docx → download final document
```

> **DB-first reads:** All business data is read from GET endpoints.  
> POST/PATCH responses return only IDs and counts — never read form data from them.

Open **`Data_Ingestion/api-docs.html`** (double-click) for the full interactive API reference.  
Import **`IntelliDraft_API.postman_collection.json`** into Postman for ready-to-run requests.

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `gemini` | Active LLM: `gemini` (primary) \| `azure_gpt5` (fallback) |
| `GEMINI_API_KEY` | — | Google Gemini API key (get free at aistudio.google.com) |
| `GOOGLE_API_KEY` | — | Alternative name for `GEMINI_API_KEY` — either works |
| `GEMINI_VERTEX_MODEL` | `gemini-2.5-flash` | Gemini model ID |
| `LOCAL_MODE` | `true` | `true` = save files locally; `false` = Azure Blob + Cosmos |
| `LOCAL_DB` | `true` | `true` = SQLite; `false` = PostgreSQL / Azure SQL via `DATABASE_URL` |
| `ASYNC_GENERATION` | `true` | `true` = background thread (poll for status); `false` = synchronous |
| `VISION_ENABLED` | `true` | `true` = AI describes extracted images |
| `AZURE_GPT5_OPENAI_API_KEY` | — | Azure OpenAI API key (fallback LLM) |
| `AZURE_GPT5_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint URL |
| `AZURE_GPT5_MODEL_DEPLOYMENT_ID` | — | Azure deployment name |
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
# ── Gemini (primary — recommended) ────────────────────────────────
MODEL_PROVIDER=gemini

# Option A: API Key (free, easiest for local dev)
GEMINI_API_KEY=your-gemini-api-key-here    # https://aistudio.google.com/app/apikey

# Option B: Vertex AI (enterprise — place key.json at Data_Ingestion/key.json)
# GOOGLE_KEY_JSON_PATH=                    # leave blank for default path

# ── Azure GPT-5 (fallback / enterprise) ───────────────────────────
# MODEL_PROVIDER=azure_gpt5
# AZURE_GPT5_OPENAI_API_KEY=your-azure-key-here
# AZURE_GPT5_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
# AZURE_GPT5_MODEL_DEPLOYMENT_ID=your-deployment-name
```

No code changes needed — the provider switch is purely config-driven.  
Gemini is tried first for every operation; Azure GPT-5 is the automatic fallback.

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
| REST API (20+ endpoints) | ✅ Complete |
| `POST /api/projects/draft` — partial save without validation | ✅ Complete |
| DB-first data flow — all reads via GET endpoints | ✅ Complete |
| Project + DerivedData ORM models | ✅ Complete |
| AI-driven field derivation (`derive-fields`) | ✅ Complete |
| Document generation (BRD, RFP, SOW, Proposal, TechSpec, Scope) | ✅ Complete |
| Google ADK multi-agent system (Orchestrator + 3 sub-agents) | ✅ Complete |
| Gemini primary / Azure GPT-5 fallback for all agents | ✅ Complete |
| Frontend (single-page, DB-first, draft persistence) | ✅ Complete |
| Production deployment | 🔄 Pending |
