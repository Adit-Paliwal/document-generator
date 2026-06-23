# IntelliDraft — LibreOffice Preview Setup Guide

## What this is

IntelliDraft renders document previews using **LibreOffice headless** on the server.
When a user clicks **📄 Preview**, the backend:

1. Assembles the current sections into a DOCX file
2. Submits a Celery task to convert it to HTML via `soffice --headless`
3. Caches the result in Redis (keyed by document version hash)
4. Streams the self-contained HTML to the browser, rendered in an iframe

LibreOffice was chosen over browser-side DOCX renderers because it produces
**pixel-accurate output** — the same engine that generates the exported Word file.

---

## Architecture

```
Browser
  │  GET /api/generate/{job_id}/preview/html
  │
  ▼
Flask API (Gunicorn)
  │  cache miss → submit task
  │  cache hit  → return HTML immediately
  ▼
Celery Worker (--concurrency=4)          Redis
  │  picks up task from queue  ◄────────────────┐
  │  runs LibreOffice headless                   │
  │  stores HTML in Redis cache ─────────────────┘
  ▼
LibreOffice (soffice --headless)
  DOCX → HTML (self-contained, CSS + images inlined)
```

**Key design decisions:**

| Decision | Reason |
|---|---|
| Celery + Redis (not threading) | Multiple parallel conversions without LibreOffice profile-lock conflicts |
| Unique `-env:UserInstallation` per task | Each Celery worker process gets its own LO profile dir → true parallelism |
| Redis cache keyed by version hash | Re-converts only when sections change; instant on cache hit |
| Blob URL iframe (not `srcdoc`) | No 2 MB browser size limit; browser handles large documents correctly |
| `CELERY_ENABLED=false` fallback | Local dev works synchronously — no Redis or worker required |

---

## Step 1 — Install LibreOffice

### Linux (Ubuntu / Debian — Docker & servers)

```bash
apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-common \
    fonts-liberation
```

> `fonts-liberation` provides Liberation Sans/Serif/Mono — metric-compatible
> replacements for Arial, Times New Roman, and Courier New. Without them,
> LibreOffice substitutes random fonts and the layout shifts.

### Windows (local development)

**Option A — winget (recommended, run in an admin PowerShell):**

```powershell
winget install TheDocumentFoundation.LibreOffice
```

**Option B — manual download:**

1. Go to <https://www.libreoffice.org/download/download/>
2. Download the **Windows 64-bit** installer (.msi)
3. Run the installer with default options

**After installing on Windows, add to PATH:**

```powershell
# Run once in an admin PowerShell:
$lo = "C:\Program Files\LibreOffice\program"
[Environment]::SetEnvironmentVariable("PATH", "$env:PATH;$lo", "Machine")
```

Then **restart your terminal** and verify:

```powershell
soffice --version
# Expected output: LibreOffice 7.x.y (something like that)
```

### macOS (local development)

```bash
brew install --cask libreoffice
# soffice will be at /Applications/LibreOffice.app/Contents/MacOS/soffice
# The preview_service.py already checks this path automatically.
```

---

## Step 2 — Install Python dependencies

```bash
pip install celery==5.5.2 redis==5.2.1
# Or install everything at once:
pip install -r Data_Ingestion/requirements.txt
```

---

## Step 3 — Local development (without Docker)

Local dev uses **synchronous mode** — no Redis or Celery needed.
Set in `Data_Ingestion/.env`:

```env
CELERY_ENABLED=false
```

Start the API server as usual:

```bash
cd Data_Ingestion
python run_server.py
```

Click Preview in the chat UI — conversion happens synchronously in the request
(takes 2–5 s for the first render; subsequent renders are fast because the
document isn't changing).

---

## Step 4 — Production with Docker Compose

### Copy and fill in the env file

```bash
cp Data_Ingestion/.env.example Data_Ingestion/.env
# Edit .env — fill in API keys, set:
# CELERY_ENABLED=true
# REDIS_URL=redis://redis:6379/0   ← docker-compose sets this automatically
```

### Build and start all services

```bash
docker-compose up -d --build
```

This starts **three services**:

| Service | Role |
|---|---|
| `redis` | Broker + result backend + HTML cache |
| `celery-worker` | LibreOffice conversion pool (concurrency=4) |
| `document-generator-api` | Flask API (Gunicorn, 2 workers × 4 threads) |

### Verify everything is running

```bash
docker-compose ps
# All three services should show "Up (healthy)"

# Check the Celery worker is connected and ready:
docker-compose logs celery-worker | grep "ready"
# Expected: [tasks.celery-worker@xxx] ready.
```

### Test the preview endpoint

```bash
# Replace <JOB_ID> with an actual job ID from a completed generation
curl http://localhost:7071/api/generate/<JOB_ID>/preview/html
# If conversion is cached: {"status":"ready","html":"<!DOCTYPE...","cached":true}
# If first request:        {"status":"pending","task_id":"abc123","poll_url":"..."}
```

---

## Step 5 — Production on Azure / GCP Cloud Run

### Azure Container Apps

The Dockerfile already installs LibreOffice. No extra steps needed for the API container.

For the Celery worker, deploy the **same image** with a different start command:

```
celery -A celery_app worker --queues=preview --concurrency=4 --loglevel=info
```

For Redis, provision **Azure Cache for Redis** (Basic C0 tier is sufficient for
<50 concurrent users). Set the connection string:

```env
REDIS_URL=rediss://:your_access_key@your_host.redis.cache.windows.net:6380/0
CELERY_ENABLED=true
```

### GCP Cloud Run

Cloud Run runs a single container — you cannot run the Celery worker in the same
container as the API. Deploy two Cloud Run services:

| Service | Image | Start command |
|---|---|---|
| `intellidraft-api` | Same image | `gunicorn run_server:app …` |
| `intellidraft-worker` | Same image | `celery -A celery_app worker --queues=preview --concurrency=4` |

Use **Memorystore for Redis** (or Redis on Cloud Run sidecar) and set `REDIS_URL`.

---

## Configuration reference

All variables go in `Data_Ingestion/.env`:

| Variable | Default | Description |
|---|---|---|
| `CELERY_ENABLED` | `false` | `true` = async Celery mode (production); `false` = sync fallback (local dev) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string (broker + cache) |
| `PREVIEW_CACHE_TTL` | `3600` | Seconds to keep cached HTML in Redis (1 hour) |
| `LO_CONVERT_TIMEOUT` | `90` | Seconds before LibreOffice conversion is killed |

---

## Troubleshooting

### "LibreOffice (soffice) not found"

`preview_service.py` checks these paths in order:

- `soffice` on `$PATH`
- `/usr/bin/soffice`
- `/usr/lib/libreoffice/program/soffice`
- `/opt/libreoffice/program/soffice`
- `C:\Program Files\LibreOffice\program\soffice.exe`
- `/Applications/LibreOffice.app/Contents/MacOS/soffice`

Fix: install LibreOffice and ensure one of the above paths exists, or add
`soffice` to your `PATH`.

### Preview times out after 60 s (frontend)

The Celery task has a hard 120 s timeout. If conversion consistently takes >60 s:

1. Increase `LO_CONVERT_TIMEOUT` in `.env`
2. Increase `task_time_limit` in `celery_app.py`
3. Check the Celery worker logs: `docker-compose logs -f celery-worker`

### Preview shows blank or broken layout

LibreOffice HTML output depends on fonts. Ensure `fonts-liberation` is installed
in Docker (already in the Dockerfile). On Windows, verify that Arial and Times New
Roman are available system fonts.

### Cache not invalidating after editing a section

Cache invalidation runs inside `update_section_content()` in
`generation_service.py`. If Redis is unreachable, it fails silently and logs a
warning. Check:

```bash
docker-compose logs celery-worker | grep "Cache invalidat"
redis-cli -u $REDIS_URL ping   # should return PONG
```

### Running multiple Celery workers

Scale the worker container to add more parallelism:

```bash
docker-compose up -d --scale celery-worker=2
# Now 2 containers × 4 concurrency = 8 parallel LibreOffice processes
```

---

## How cache invalidation works

Every section version update (`PATCH /api/generate/{job_id}/section/{id}`) calls
`invalidate_preview_cache(job_id)`, which deletes all Redis keys matching
`preview:{job_id}:*` using `SCAN + DEL`.

The cache key also embeds a **version hash** (MD5 of all section IDs + version
numbers), so even without explicit invalidation, editing a section produces a
different key and the old HTML expires via TTL automatically.

---

*Last updated: 2026-06-23*
