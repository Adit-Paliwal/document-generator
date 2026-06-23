# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Backend API Docker Image
#
# Entry point : Data_Ingestion/run_server.py  (Flask, no Azure Functions needed)
# Port        : 7071
# Python      : 3.11-slim  (matches tested version)
#
# Build:   docker build -t document-generator-api .
# Run:     docker-compose up -d
#
# Services (docker-compose):
#   document-generator-api  — Flask API + Gunicorn, port 7071
#   celery-worker           — LibreOffice DOCX→HTML preview conversions
#   redis                   — Celery broker, result backend, preview HTML cache
#
# LLM providers:
#   Primary  — Gemini 2.5 Flash (Vertex AI) — mount key.json via docker-compose volume
#   Fallback — Azure GPT-5 — set AZURE_GPT5_* env vars in Data_Ingestion/.env
#
# Features in this image:
#   - Document generation (BRD, RFP, NIT, BOQ, NDPR, NFA, ARB)
#   - LibreOffice DOCX→HTML preview with true inline editing
#   - Section versioning + DocumentSnapshot save/restore
#   - Paginated project list, N+1-free job queries, version_hash caching
#   - Celery async preview tasks (CELERY_ENABLED=true) or sync fallback
#
# Frontend (chat.html / index.html) is served separately — NOT in this image.
#   Local dev:  open frontend/index.html directly in browser
#   Production: serve via nginx or CDN; point BASE_URL to this API
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# ── System libraries ─────────────────────────────────────────────────────────
# Base libs required by PyMuPDF / lxml / cryptography
# LibreOffice Writer + fonts for DOCX→HTML preview conversion
#   - libreoffice-writer : the Writer component (converts DOCX)
#   - libreoffice-common : shared runtime files
#   - fonts-liberation   : metric-compatible replacements for Arial/Times/Courier
#                          (prevents missing-font warnings in converted documents)
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

# ── Python dependencies ───────────────────────────────────────────────────────
# pywin32 is Windows-only — strip it before installing on Linux
COPY Data_Ingestion/requirements.txt /tmp/requirements.txt
RUN grep -v "pywin32" /tmp/requirements.txt > /tmp/req_linux.txt \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/req_linux.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY Data_Ingestion/ /app/Data_Ingestion/

# Pre-create local storage so it exists even before the volume is mounted
RUN mkdir -p /app/Data_Ingestion/local_storage

# ── Working directory for the server ─────────────────────────────────────────
WORKDIR /app/Data_Ingestion

# PORT env var:
#   Cloud Run injects $PORT automatically (usually 8080).
#   Local Docker / docker-compose: falls back to 7071.
#   The health check and EXPOSE both use 7071 for local compat;
#   Cloud Run overrides via the $PORT variable at runtime.
EXPOSE 7071

# ── Health check — lightweight /api/health (zero DB cost) ────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-7071}/api/health > /dev/null || exit 1

# ── Start with Gunicorn (production WSGI) ────────────────────────────────────
# --workers=2          : 2 processes (enough for staging/prod single-instance)
# --worker-class=gthread: thread-based workers — safe with SQLite WAL + background gen threads
# --threads=4          : 4 threads per worker → handles concurrent polls during generation
# --timeout=200        : derive-fields uses 180s LLM timeout; 200s gives headroom above that
# --access-logfile=-   : stream access logs to stdout (captured by Cloud Run / Docker)
# --bind uses $PORT    : Cloud Run sets PORT; local falls back to 7071
CMD exec gunicorn \
    --workers=2 \
    --worker-class=gthread \
    --threads=4 \
    --bind=0.0.0.0:${PORT:-7071} \
    --timeout=200 \
    --access-logfile=- \
    run_server:app
