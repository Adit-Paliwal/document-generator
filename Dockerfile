# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Backend API Docker Image
#
# Entry point : Data_Ingestion/run_server.py  (Flask, no Azure Functions needed)
# Port        : 7071
# Python      : 3.11-slim  (matches tested version)
#
# Build:   docker build -t intellidraft-api .
# Run:     docker-compose up -d
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

# ── Health check ─────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:7071/api/templates > /dev/null || exit 1

# ── Start Flask server ────────────────────────────────────────────────────────
CMD ["python", "run_server.py"]
