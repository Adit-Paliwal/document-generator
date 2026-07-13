# IntelliDraft — Azure Databricks Phase 1 Deployment Guide

> **All data lives in Databricks only** — no GCS, no Redis, no PostgreSQL.  
> FastAPI (main.py) → Databricks Apps · DB → Databricks SQL Warehouse · Files → Unity Catalog Volumes
> Frontend: build the React app (`cd frontend-react && npm run build`) and copy
> `frontend-react/dist` → `Data_Ingestion/frontend-react-dist` before syncing.

---

## What you need before starting

| Requirement | Notes |
|---|---|
| Azure subscription | Must have permissions to create resource groups |
| Azure Databricks workspace | **Premium tier required** (Apps need Premium) |
| Python 3.11 | On your local machine |
| Databricks CLI v0.200+ | Install steps in Section 1 |
| GCP service account JSON | With `roles/aiplatform.user` for Vertex AI / Gemini |
| The IntelliDraft code zip | `intellidraft_changes_2.zip` + `intellidraft_databricks_phase1.zip` (both) |

---

## Section 1 — Install Databricks CLI

```bash
# Windows (PowerShell)
winget install Databricks.DatabricksCLI

# Mac / Linux
curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh

# Verify
databricks --version
# Should print: Databricks CLI v0.200.0 or higher
```

---

## Section 2 — Create Azure Databricks Workspace (if not done)

1. Go to **portal.azure.com** → Create a resource → search **Azure Databricks**
2. Click **Create**
3. Fill in:
   - **Workspace name**: `intellidraft-workspace`
   - **Region**: choose the region closest to your users
   - **Pricing tier**: **Premium** ← required for Databricks Apps
4. Click **Review + Create** → **Create**
5. Wait 2–3 minutes for provisioning
6. Click **Launch Workspace** — note the workspace URL:
   `https://<your-workspace-id>.azuredatabricks.net`

---

## Section 3 — Configure Databricks CLI

```bash
# Replace the URL with your actual workspace URL
databricks configure --host https://<your-workspace-id>.azuredatabricks.net

# When prompted for token:
# 1. In the Databricks workspace UI → click your username (top right)
# 2. Click "User Settings" → "Developer" → "Access Tokens"
# 3. Click "Generate new token" → give it a name → click "Generate"
# 4. Copy the token (starts with dapi_) and paste it in the terminal

# Verify the CLI works
databricks workspace list /
```

---

## Section 4 — Create SQL Warehouse

1. In the Databricks workspace UI → left sidebar → **SQL Warehouses**
2. Click **Create SQL Warehouse**
3. Settings:
   - **Name**: `intellidraft-warehouse`
   - **Cluster size**: `Small` (2 DBUs) — enough for Phase 1
   - **Warehouse type**: **Serverless** ← recommended (auto-scale, instant start)
   - **Auto stop**: `10 minutes`
4. Click **Create**
5. Wait for it to start (turns green)
6. Click the warehouse → **Connection details** tab
7. Note these two values (you will need them):
   - **Server hostname**: `<workspace-id>.azuredatabricks.net`
   - **HTTP path**: `/sql/1.0/warehouses/<warehouse-id>`

---

## Section 5 — Set Up Unity Catalog (auto-enabled)

Unity Catalog is automatically enabled on all Azure Databricks workspaces created after November 2023. Verify it is enabled:

1. Workspace UI → **Data** (left sidebar) → you should see a catalog tree
2. If you see **hive_metastore** only (no Unity Catalog), contact your Azure admin to enable it

---

## Section 6 — Create Catalog, Schemas, and Volume

Open a **SQL Editor** in the Databricks workspace (left sidebar → SQL Editor) and run these SQL statements one by one:

```sql
-- 1. Create the catalog
CREATE CATALOG IF NOT EXISTS intellidraft
COMMENT 'IntelliDraft AI document generation platform';

-- 2. Create schemas (databases)
CREATE SCHEMA IF NOT EXISTS intellidraft.ops
COMMENT 'Operational data — generation jobs, sections, chat sessions';

CREATE SCHEMA IF NOT EXISTS intellidraft.cache
COMMENT 'HTML preview cache (Phase 2)';

-- 3. Create the Volume for file storage (replaces GCS)
CREATE VOLUME IF NOT EXISTS intellidraft.ops.files
COMMENT 'IntelliDraft document blobs — uploads, images, exports';
```

Verify the volume was created:
```sql
SHOW VOLUMES IN intellidraft.ops;
-- Should show: intellidraft | ops | files
```

The full path to your volume is:
```
/Volumes/intellidraft/ops/files
```

> **Note:** Update `DATABRICKS_VOLUME_PATH` in `app.yaml` if you use a different catalog/schema/volume name.

---

## Section 7 — Create Secrets Scope and Store All Secrets

A secrets scope keeps all sensitive values out of the code and out of `app.yaml`.

### 7a. Create the scope

```bash
databricks secrets create-scope intellidraft
```

### 7b. Store all secrets (run each command separately)

```bash
# Databricks connection — for SQLAlchemy DATABASE_URL
# Format: databricks://token:<TOKEN>@<HOST>?http_path=<PATH>&catalog=intellidraft&schema=ops
databricks secrets put-secret intellidraft DATABASE_URL \
  --string-value "databricks://token:dapi_XXXX@<workspace>.azuredatabricks.net?http_path=/sql/1.0/warehouses/<wh-id>&catalog=intellidraft&schema=ops"

# Databricks SQL connection parts (used by databricks-sdk WorkspaceClient)
databricks secrets put-secret intellidraft DATABRICKS_TOKEN \
  --string-value "dapi_XXXXXXXXXXXXXXXXXXXXXXXXXXXX"

databricks secrets put-secret intellidraft DATABRICKS_SERVER_HOSTNAME \
  --string-value "<workspace-id>.azuredatabricks.net"

databricks secrets put-secret intellidraft DATABRICKS_HTTP_PATH \
  --string-value "/sql/1.0/warehouses/<warehouse-id>"

# GCP / Vertex AI (for Gemini LLM calls — cross-cloud, outbound HTTPS)
databricks secrets put-secret intellidraft VERTEX_AI_PROJECT \
  --string-value "your-gcp-project-id"

databricks secrets put-secret intellidraft VERTEX_AI_LOCATION \
  --string-value "us-central1"

# GCP service account JSON — paste the entire JSON as one line
# (open the .json file, copy all content, paste as the value)
databricks secrets put-secret intellidraft GOOGLE_APPLICATION_CREDENTIALS_JSON \
  --string-value '{"type":"service_account","project_id":"..."}'

# (SECRET_KEY retired with Flask — no session secret needed for the FastAPI app)
```

Verify the scope was created:
```bash
databricks secrets list-secrets intellidraft
# Should show all 8 secret keys
```

---

## Section 8 — Prepare and Upload the Code

### 8a. Extract the code

1. Extract `intellidraft_changes_2.zip` (previous changes)
2. Extract `intellidraft_databricks_phase1.zip` on top — overwrite if prompted
3. You should now have the `Data_Ingestion/` folder with all updated files

### 8b. Install Databricks CLI sync

The sync command uploads your local folder to the Databricks workspace:

```bash
# Navigate to the Data_Ingestion folder
cd path/to/Data_Ingestion

# Sync to workspace (replace email with your Databricks user email)
databricks sync . /Workspace/Users/your-email@company.com/intellidraft-api

# You should see output like:
# Uploaded app.yaml
# Uploaded main.py
# Uploaded storage/databricks_volume_storage.py
# ... (all files, including frontend-react-dist/ and ontology/)
```

---

## Section 9 — Create and Deploy the Databricks App

### 9a. Create the app

```bash
# Create the app (only needed once)
databricks apps create intellidraft-api

# You should see:
# App 'intellidraft-api' created.
```

### 9b. Add sensitive environment variables via UI

The `app.yaml` already sets non-sensitive env vars. Now add the sensitive ones:

1. In the Databricks workspace → **Apps** (left sidebar, or app switcher)
2. Find **intellidraft-api** → click it
3. Click **Settings** (gear icon)
4. Under **Environment variables**, click **Add variable** for each:

| Variable Name | Value |
|---|---|
| `DATABASE_URL` | (from secrets — see Section 7b format) |
| `DATABRICKS_SERVER_HOSTNAME` | `<workspace>.azuredatabricks.net` |
| `DATABRICKS_HTTP_PATH` | `/sql/1.0/warehouses/<wh-id>` |
| `DATABRICKS_TOKEN` | `dapi_XXXXXXXXXXXXXXXXXXXXXXXXXXXX` |
| `VERTEX_AI_PROJECT` | `your-gcp-project-id` |
| `VERTEX_AI_LOCATION` | `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | *(full GCP service account JSON as string)* |
| `SECRET_KEY` | *(any long random string)* |

5. Click **Save**

### 9c. Deploy the app

```bash
databricks apps deploy intellidraft-api \
  --source-code-path /Workspace/Users/your-email@company.com/intellidraft-api

# Deployment takes 2–4 minutes (installs requirements.txt, starts gunicorn)
# You should see:
# App 'intellidraft-api' deployed successfully.
# App URL: https://intellidraft-api-<id>.databricksapps.com
```

### 9d. Get the App URL

```bash
databricks apps get intellidraft-api
# Look for "url" in the output — this is your API base URL
```

---

## Section 10 — Verify the Database Tables Were Created

The first boot of the FastAPI app calls `Base.metadata.create_all(engine)` which creates all tables automatically (projects, jobs, sections, versions, comments, reviews, personas, users, notifications, …). Verify in SQL Editor:

```sql
USE CATALOG intellidraft;
USE SCHEMA ops;
SHOW TABLES;

-- Expected output:
-- chat_sessions
-- generation_jobs
-- projects
-- section_comments
-- section_versions
-- sections
-- templates
```

If the tables are missing, check the App logs:
```bash
databricks apps logs intellidraft-api
```

---

## Section 11 — Test the Deployment

Replace `<APP_URL>` with the URL from Section 9d.

```bash
# Health check
curl https://<APP_URL>/api/health

# Expected response:
# {"status": "ok", "timestamp": "..."}

# Test document upload (replace path with an actual PDF)
curl -X POST https://<APP_URL>/api/documents/upload \
  -F "file=@/path/to/test.pdf"

# Expected response:
# {"document_id": "...", "status": "parsed"}

# Check that the file appears in the UC Volume
# In SQL Editor:
LIST '/Volumes/intellidraft/ops/files/documents/';
```

---

## Section 12 — Redeploy After Code Changes

Whenever the code is updated:

```bash
# Sync changes to workspace
databricks sync . /Workspace/Users/your-email@company.com/intellidraft-api

# Redeploy
databricks apps deploy intellidraft-api \
  --source-code-path /Workspace/Users/your-email@company.com/intellidraft-api
```

---

## Troubleshooting

### App fails to start

```bash
# Check deployment logs
databricks apps logs intellidraft-api

# Common issues:
# 1. "ModuleNotFoundError: databricks" → requirements.txt install failed
#    Fix: check that requirements.txt has databricks-sqlalchemy and databricks-sdk
#
# 2. "KeyError: DATABASE_URL" → env var not set
#    Fix: add DATABASE_URL in Apps UI (Section 9b)
#
# 3. "Invalid token" → DATABRICKS_TOKEN incorrect
#    Fix: regenerate PAT, update in Apps UI
```

### Database tables not created

```bash
# Check if SQLAlchemy can connect
# In the App logs look for "[db] engine created" message
# If you see "connection refused" or "auth failed":
# - Verify DATABASE_URL format exactly matches:
#   databricks://token:<TOKEN>@<HOST>?http_path=<PATH>&catalog=intellidraft&schema=ops
# - Verify the SQL Warehouse is running (not stopped)
```

### File upload fails (UC Volume error)

```bash
# Verify volume path in env vars:
# DATABRICKS_VOLUME_PATH should be /Volumes/intellidraft/ops/files

# Verify the volume exists:
# SQL Editor → LIST '/Volumes/intellidraft/ops/files/'

# If "PERMISSION_DENIED":
# The app's service principal needs WRITE access to the volume
# In SQL Editor:
GRANT WRITE VOLUME ON VOLUME intellidraft.ops.files TO `<app-service-principal-email>`;
```

### Vertex AI / Gemini calls failing

```bash
# The App makes outbound HTTPS calls to GCP Vertex AI from Azure — this works by default
# If calls fail, check:
# 1. GOOGLE_APPLICATION_CREDENTIALS_JSON is valid (try parsing the JSON)
# 2. VERTEX_AI_PROJECT is your actual GCP project ID
# 3. The service account has roles/aiplatform.user in GCP
```

---

## Environment Variables — Complete Reference

| Variable | Set in | Required | Description |
|---|---|---|---|
| `LOCAL_DB` | app.yaml | Yes | Set to `false` for Databricks |
| `LOCAL_MODE` | app.yaml | Yes | Set to `false` for Databricks |
| `DATABRICKS_MODE` | app.yaml | Yes | Set to `true` to use UC Volumes |
| `CELERY_ENABLED` | app.yaml | Yes | Set to `false` (no Celery in Phase 1) |
| `DATABRICKS_CATALOG` | app.yaml | Yes | `intellidraft` |
| `DATABRICKS_SCHEMA` | app.yaml | Yes | `ops` |
| `DATABRICKS_VOLUME_PATH` | app.yaml | Yes | `/Volumes/intellidraft/ops/files` |
| `DATABASE_URL` | Apps UI | Yes | Full databricks:// SQLAlchemy URL |
| `DATABRICKS_SERVER_HOSTNAME` | Apps UI | Yes | Workspace hostname (no https://) |
| `DATABRICKS_HTTP_PATH` | Apps UI | Yes | SQL Warehouse HTTP path |
| `DATABRICKS_TOKEN` | Apps UI | Yes | Personal access token (dapi_...) |
| `VERTEX_AI_PROJECT` | Apps UI | Yes | GCP project ID |
| `VERTEX_AI_LOCATION` | Apps UI | Yes | e.g. `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | Apps UI | Yes | Full GCP service account JSON |
| `GENERATION_CONCURRENCY` | Apps UI | No | Parallel section generation (default 4) |
| `CORS_ALLOW_ORIGINS` | Apps UI | No | Restrict origins in production (default `*`) |
| `THREADPOOL_TOKENS` | Apps UI | No | Sync-endpoint threadpool size (default 80) |

---

## Key files (post-cleanup, 2026-07-13)

| File | Role |
|---|---|
| `Data_Ingestion/main.py` | The FastAPI server — the ONLY API entry point |
| `Data_Ingestion/app.yaml` | Databricks Apps config (gunicorn + uvicorn workers) |
| `Data_Ingestion/frontend-react-dist/` | Built React SPA (copy of `frontend-react/dist`) |
| `Data_Ingestion/ontology/*.json` | Business ontology pack (prompt grounding) |
| `Data_Ingestion/storage/databricks_volume_storage.py` | Unity Catalog Volume storage service |

Removed in the cleanup: Flask (`run_server.py`), Celery/Redis preview workers,
Cloud Run + GCP deploy scripts, Vertex AI Agent Engine deploy scripts.

Other supported target: **Docker / self-host** (`Dockerfile` + `docker-compose.yml`,
FastAPI multi-stage). See [SETUP.md](SETUP.md) → Option B.

---

## Phase 2 (Next Steps — After Phase 1 is Stable)

Once Phase 1 is running successfully:

1. **Background generation → Databricks Jobs**: Replace `threading.Thread` with a Databricks Job triggered via REST API. Status polling works unchanged (both read the same SQL Warehouse table).
2. **LibreOffice preview → Databricks Jobs**: Create a Job with an init script that installs LibreOffice (`sudo apt-get install -y libreoffice`). Remove sync preview limitation.
3. **Preview cache → Delta table**: Replace in-memory `_preview_cache` dict with a `intellidraft.cache.preview_cache` Delta table — persists across App restarts and multiple instances.
