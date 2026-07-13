# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Backend + Frontend Docker Image (FastAPI, single container)
#
# Entry point : Data_Ingestion/main.py (FastAPI via gunicorn + uvicorn workers)
# Port        : 7071 local · $PORT in cloud
# Python      : 3.11-slim
#
# What this image contains:
#   - FastAPI API server (main.py) — the only API server
#   - The React SPA (frontend-react), built in stage 1 and served by FastAPI
#   - LibreOffice Writer for DOCX→HTML preview (synchronous; markdown2 fallback)
#   - Google ADK multi-agent system + parsers + document generation
#
# Build:  docker build -t intellidraft .
# Run:    docker compose up -d      (see docker-compose.yml)
#         docker run -p 7071:7071 --env-file Data_Ingestion/.env intellidraft
#
# The preview stack is fully synchronous — no Celery, no Redis. Generation runs
# wave-parallel inside the app (GENERATION_CONCURRENCY, default 4).
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: build the React SPA ─────────────────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /ui
COPY frontend-react/package.json frontend-react/package-lock.json* ./
RUN npm install
COPY frontend-react/ ./
RUN npm run build          # → /ui/dist

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.11-slim

# System libs: PyMuPDF/lxml/crypto + LibreOffice Writer for the preview renderer
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        curl \
        libreoffice-writer \
        libreoffice-common \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies (pywin32 is Windows-only — strip before installing on Linux)
COPY Data_Ingestion/requirements.txt /tmp/requirements.txt
RUN grep -v "pywin32" /tmp/requirements.txt > /tmp/req_linux.txt \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/req_linux.txt

# Application code
COPY Data_Ingestion/ /app/Data_Ingestion/

# Built SPA from stage 1 → served by FastAPI at /  (main.py checks this path first)
COPY --from=frontend /ui/dist /app/Data_Ingestion/frontend-react-dist

# Pre-create local storage so it exists even before a volume is mounted
RUN mkdir -p /app/Data_Ingestion/local_storage

WORKDIR /app/Data_Ingestion

# PORT: cloud platforms inject $PORT; local Docker falls back to 7071
EXPOSE 7071

# Health check — lightweight /api/health (zero DB cost)
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-7071}/api/health > /dev/null || exit 1

# ── Start: gunicorn + uvicorn ASGI workers (FastAPI) ─────────────────────────
# --worker-class uvicorn.workers.UvicornWorker : ASGI worker for main:app.
#   Sync `def` endpoints run in each worker's threadpool, so blocking
#   SQLAlchemy/litellm calls never block the event loop.
# --timeout 200 : derive-fields uses a 180s LLM timeout; 200s gives headroom.
CMD exec gunicorn \
    --workers=2 \
    --worker-class=uvicorn.workers.UvicornWorker \
    --bind=0.0.0.0:${PORT:-7071} \
    --timeout=200 \
    --access-logfile=- \
    main:app
