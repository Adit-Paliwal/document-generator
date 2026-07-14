# IntelliDraft — Databricks Deployment Process (Backend + Frontend together)

This is the practical, step-by-step process to deploy IntelliDraft to **Azure
Databricks Apps**. Backend (FastAPI) and frontend (React SPA) ship as **one
unit** — the API server serves the built React app at `/`.

> Full reference: `DATABRICKS_DEPLOY_GUIDE.md`. This file is the short "how to
> ship a build" checklist + the **database / tables** answer.

---

## 0. The deployable unit

Databricks Apps deploys **one folder**: `Data_Ingestion/`. It contains
`app.yaml` (the app config), `main.py` (FastAPI entry point), `requirements.txt`,
`ontology/`, and — after the build step below — `frontend-react-dist/` (the
compiled React app). Everything the app needs is inside `Data_Ingestion/`; you
do **not** deploy the repo root.

```
Data_Ingestion/                ← this whole folder is what you sync to Databricks
├── app.yaml                   ← Databricks Apps config (gunicorn + uvicorn, main:app)
├── main.py                    ← FastAPI server (serves API + the React SPA)
├── requirements.txt
├── ontology/                  ← business knowledge pack (JSON)
├── frontend-react-dist/       ← ★ built React app (you create this in Step 1)
├── agents/  generation/  api/  parsers/  storage/  models/
└── ...
```

---

## 1. Build the frontend and place it inside the backend

From the repo root, on your machine (needs Node 20+):

```bash
cd frontend-react
npm install
npm run build                 # → frontend-react/dist

# copy the build into the backend so it ships in the same deploy unit:
cd ..
# Windows PowerShell:
Remove-Item -Recurse -Force Data_Ingestion\frontend-react-dist -ErrorAction SilentlyContinue
Copy-Item -Recurse frontend-react\dist Data_Ingestion\frontend-react-dist
# macOS/Linux:
# rm -rf Data_Ingestion/frontend-react-dist && cp -r frontend-react/dist Data_Ingestion/frontend-react-dist
```

`main.py` automatically serves `Data_Ingestion/frontend-react-dist` at `/` when
it exists (it checks that path first). No backend code change needed.

---

## 2. Sync the code to your Databricks workspace

```bash
cd Data_Ingestion
databricks sync . /Workspace/Users/<your-email>/intellidraft-api
```

You should see `app.yaml`, `main.py`, `frontend-react-dist/...`, `ontology/...`
and all backend files upload.

---

## 3. Configure secrets & env (once, in the Databricks Apps UI)

`app.yaml` already sets the non-sensitive values (DATABRICKS_MODE, catalog,
schema, volume path). Set these **sensitive** ones in the App's **Environment**
tab (or a secret scope):

| Variable | Value |
|---|---|
| `DATABRICKS_SERVER_HOSTNAME` | `<workspace>.azuredatabricks.net` (no https://) |
| `DATABRICKS_HTTP_PATH` | `/sql/1.0/warehouses/<warehouse-id>` |
| `VERTEX_AI_PROJECT` | your GCP project id (Gemini) |
| `VERTEX_AI_LOCATION` | e.g. `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | full contents of the GCP service-account JSON |

Leave `DATABASE_URL` / `DATABRICKS_TOKEN` **unset** — the App injects its
service-principal OAuth credentials and `db.py` builds the SQL Warehouse
connection from them automatically (no PAT needed).

**Optional production hardening:**
`CORS_ALLOW_ORIGINS=https://<your-app-url>` · `ENABLE_ADMIN_ENDPOINTS` unset
(keeps the DB-reset endpoint disabled) · `GENERATION_CONCURRENCY=4`.

---

## 4. Deploy

```bash
databricks apps deploy intellidraft-api \
  --source-code-path /Workspace/Users/<your-email>/intellidraft-api
```

Deployment takes ~2–4 min (installs `requirements.txt`, starts gunicorn). When
it's up, open the App URL:
- `/` → the React app
- `/api/health` → `{"status":"ok"}`
- `/docs` → interactive API reference

---

## 5. Database tables — do you need to create them? **No (fresh deploy).**

**On first boot the app creates every table automatically.** `main.py` →
`get_engine()` calls `Base.metadata.create_all(engine)`, which is **idempotent**
and creates any missing table in your Databricks SQL Warehouse schema. You do
**not** write any `CREATE TABLE` by hand.

**The one prerequisite** — the App's service principal must be allowed to create
and use tables in the target schema. Run once in the SQL Editor:

```sql
GRANT USE CATALOG ON CATALOG <catalog>                              TO `<app-service-principal>`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY, SELECT ON SCHEMA <catalog>.<schema>  TO `<app-service-principal>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME <catalog>.<schema>.<volume>        TO `<app-service-principal>`;
-- plus "Can use" on the SQL Warehouse (Warehouse → Permissions)
```

The **16 tables** created automatically:
`projects`, `derived_data`, `generation_jobs`, `sections`, `section_versions`,
`section_comments`, `document_snapshots`, `templates`, `chat_sessions`,
`users`, `personas`, `review_requests`, `review_assignments`, `review_comments`,
`review_summaries`, `notifications`.

Verify after first boot:
```sql
SHOW TABLES IN <catalog>.<schema>;
```

### 5a. Upgrading an EXISTING Databricks schema (only if you deployed an older build before)

`create_all()` adds **new tables** but **not new columns** on Databricks (the
auto-column-migration in `db.py` is SQLite-only). If you already have an older
IntelliDraft schema live, add the columns introduced in this release:

```sql
-- new table is created automatically by create_all():  notifications
-- new columns to add manually on an existing schema:
ALTER TABLE <catalog>.<schema>.generation_jobs  ADD COLUMN project_id           STRING;
ALTER TABLE <catalog>.<schema>.section_versions ADD COLUMN edited_by            STRING;
ALTER TABLE <catalog>.<schema>.review_summaries ADD COLUMN comments_fingerprint STRING;
ALTER TABLE <catalog>.<schema>.projects ADD COLUMNS (
  pain_points STRING, opportunities STRING, business_justification STRING, deadline STRING,
  integration_requirement STRING, assumptions STRING, approval_matrix STRING, future_roadmap STRING,
  scalability_considerations STRING, innovation_objectives STRING, sustainability_esg STRING,
  project_type STRING
);
```

If the existing data is disposable (pilot/dev), the simplest path is to **drop
the schema and let `create_all` rebuild it** on next boot.

> **Files (Unity Catalog Volumes), not tables:** parsed documents and exports
> live in the Volume at `DATABRICKS_VOLUME_PATH` — created/managed automatically
> once the volume exists and the grants above are in place. Nothing to pre-create.

---

## 6. Redeploying after a change (e.g. this preview fix)

Frontend change → rebuild + recopy + sync + deploy:
```bash
cd frontend-react && npm run build && cd ..
Copy-Item -Recurse -Force frontend-react\dist Data_Ingestion\frontend-react-dist
cd Data_Ingestion && databricks sync . /Workspace/Users/<your-email>/intellidraft-api
databricks apps deploy intellidraft-api --source-code-path /Workspace/Users/<your-email>/intellidraft-api
```
Backend-only change → skip the build/copy, just sync + deploy. **No DB change**
is needed for the preview fix (it's frontend-only).

---

## Quick answers

- **Deploy backend + frontend together?** Yes — build the React app into
  `Data_Ingestion/frontend-react-dist`, then deploy `Data_Ingestion/` as one App.
- **Do I create tables?** No for a fresh schema — the app auto-creates all 16 on
  first boot; you only grant `CREATE TABLE` to the service principal. For an
  existing older schema, run the ALTERs in §5a.
- **New table in this release?** Only `notifications` (auto-created). Plus a few
  new columns (auto on fresh; manual ALTER on an existing schema).
