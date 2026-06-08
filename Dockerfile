# ══════════════════════════════════════════════════════════════════════════════
# Document Generator — Backend API Docker Image
#
# Entry point : Data_Ingestion/run_server.py  (Flask, no Azure Functions needed)
# Port        : 7071
# Python      : 3.11-slim  (matches tested version)
#
# Build:   docker build -t document-generator-api .
# Run:     docker-compose up -d
#
# LLM providers:
#   Primary  — Gemini 2.5 Flash (Vertex AI) — mount key.json via docker-compose volume
#   Fallback — Azure GPT-5 — set AZURE_GPT5_* env vars in Data_Ingestion/.env
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

# ── System libraries required by PyMuPDF / lxml / cryptography ───────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        curl \
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

EXPOSE 7071

# ── Health check — lightweight /api/health (zero DB cost) ────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -sf http://localhost:7071/api/health > /dev/null || exit 1

# ── Start with Gunicorn (production WSGI) ────────────────────────────────────
# --workers=2          : 2 processes (enough for staging/prod single-instance)
# --worker-class=gthread: thread-based workers — safe with SQLite WAL + background gen threads
# --threads=4          : 4 threads per worker → handles concurrent polls during generation
# --timeout=200        : derive-fields uses 180s LLM timeout; 200s gives headroom above that
# --access-logfile=-   : stream access logs to stdout (captured by Docker)
CMD ["gunicorn", \
     "--workers=2", \
     "--worker-class=gthread", \
     "--threads=4", \
     "--bind=0.0.0.0:7071", \
     "--timeout=200", \
     "--access-logfile=-", \
     "run_server:app"]
