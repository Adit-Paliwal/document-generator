# IntelliDraft — Local Setup Manual

Get the app running on a fresh machine (Windows / macOS / Linux). Two ways:
**A) Native** (best for development) or **B) Docker** (one command, nothing to install but Docker).

---

## 0. What you need

| Tool | Version | Why |
|---|---|---|
| **Python** | 3.11 or newer | backend (FastAPI) |
| **Node.js** | 20 or newer | build the React frontend |
| **Git** | any | clone the repo |
| **LibreOffice** | optional | nicer document previews (auto-falls back to a built-in renderer if absent) |
| **GCP service-account key** | — | `key.json` for Gemini (the LLM). Ask the project owner. |

> Windows only: enable Long Paths once (Admin PowerShell), then restart the terminal:
> ```powershell
> New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
>   -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
> ```

---

## 1. Clone

```bash
git clone <REPO_URL> Intellidraft
cd Intellidraft
```

The layout you care about:
```
Intellidraft/
├── Data_Ingestion/        # FastAPI backend (main.py is the entry point)
│   ├── main.py            # the API server
│   ├── requirements.txt
│   ├── ontology/          # business knowledge pack (ships with the repo)
│   ├── .env.example       # copy this to .env
│   └── key.json           # ← you add this (GCP creds, not in git)
├── frontend-react/        # React SPA (Vite + Tailwind)
├── Dockerfile             # option B
└── docker-compose.yml
```

---

## Option A — Native setup (development)

### A1. Backend — Python virtual environment

From the repo root (`Intellidraft/`):

**Windows (PowerShell):**
```powershell
python -m venv env
env\Scripts\python.exe -m pip install --upgrade pip
env\Scripts\python.exe -m pip install -r Data_Ingestion\requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv env
env/bin/pip install --upgrade pip
env/bin/pip install -r Data_Ingestion/requirements.txt
```

> First install pulls a lot (google-adk, litellm, PyMuPDF…) — give it a few minutes.

### A2. Configuration

```bash
cp Data_Ingestion/.env.example Data_Ingestion/.env
```
Then, for a **local dev run**, make sure these are set in `Data_Ingestion/.env`:
```ini
LOCAL_MODE=true            # use the local filesystem, not Databricks/cloud
DATABRICKS_MODE=false
# Gemini via Vertex AI:
VERTEX_AI_PROJECT=<your-gcp-project-id>
VERTEX_AI_LOCATION=us-central1
```
Drop the **`key.json`** GCP service-account file into `Data_Ingestion/key.json`
(the LLM provider looks there by default). Without it, uploads/parsing work but
AI generation/extraction return a clear 502.

The SQLite database is created automatically on first run. (On Windows it is
redirected out of the OneDrive-synced folder to `%LOCALAPPDATA%\Intellidraft`;
that warning at startup is expected. Override with `INTELLIDRAFT_DB_DIR`.)

### A3. Frontend — build the React app

```bash
cd frontend-react
npm install
npm run build          # outputs frontend-react/dist
cd ..
```
The FastAPI server serves this build at `/`. (You only rebuild when frontend
code changes.)

### A4. Run

**Windows:**
```powershell
env\Scripts\python.exe Data_Ingestion\main.py
```
**macOS / Linux:**
```bash
env/bin/python Data_Ingestion/main.py
```

Open **http://localhost:7071/** — you'll land on the login screen (enter any
email + name; that's the SSO placeholder). Useful URLs:

| URL | What |
|---|---|
| http://localhost:7071/ | the app (React SPA) |
| http://localhost:7071/api/health | health check → `{"status":"ok"}` |
| http://localhost:7071/docs | interactive API reference (Swagger) |

### A5. (Optional) Frontend hot-reload while developing

Instead of rebuilding after every change, run Vite's dev server in a second
terminal — it proxies `/api` to the backend on 7071:
```bash
cd frontend-react
npm run dev            # http://localhost:5173
```

---

## Option B — Docker (one command)

Needs only Docker Desktop. From the repo root:

```bash
# 1. put your GCP key at Data_Ingestion/key.json
# 2. cp Data_Ingestion/.env.example Data_Ingestion/.env  (set LOCAL_MODE=true, VERTEX_AI_*)
docker compose up -d --build
```

Open **http://localhost:7071/**. The image builds the React frontend, installs
LibreOffice for previews, and runs the FastAPI server under gunicorn+uvicorn.
Data persists in the `intellidraft_storage` volume across restarts.

```bash
docker compose logs -f      # watch logs
docker compose down         # stop
```

---

## 3. Verify it works (optional but recommended)

With the server running on port 7073 (tests default there — start a second
instance with `PORT=7073` or point `INTELLIDRAFT_BASE` at 7071):

```bash
# Unit + agent tests (no server needed):
env/bin/python -m pytest Data_Ingestion/tests/test_unit.py Data_Ingestion/tests/test_validation_agent.py -q

# Full suite incl. live-server integration (server must be up):
env/bin/python -m pytest Data_Ingestion/tests -q

# API contract regression:
env/bin/python Data_Ingestion/tests/api_contract.py http://127.0.0.1:7071 --compare
```

---

## 4. Common issues

| Symptom | Fix |
|---|---|
| `error while attempting to bind ... 10048 / address in use` | Port 7071 is taken. Kill the other process, or run on another port: `PORT=7072 python Data_Ingestion/main.py`. |
| `Missing packages ... Wrong Python interpreter?` at startup | You ran system Python, not the venv. Use `env\Scripts\python.exe` / `env/bin/python`. |
| AI generation returns 502 | `key.json` missing or `VERTEX_AI_PROJECT` unset — see A2. |
| Preview looks plain (no Word styling) | LibreOffice isn't installed — the markdown2 fallback is being used. Install LibreOffice for full fidelity. |
| `UnicodeEncodeError` in console logs (Windows) | Cosmetic only; the app forces UTF-8 on its own streams. |

---

For production deployment on **Azure Databricks**, see
[DATABRICKS_DEPLOY_GUIDE.md](DATABRICKS_DEPLOY_GUIDE.md).
