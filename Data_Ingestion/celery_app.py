# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Celery Application
#
# Broker + backend: Redis   (REDIS_URL env var)
# Queue:            preview — handles LibreOffice DOCX→HTML conversions
#
# Start worker (inside the container or locally):
#   celery -A celery_app worker --queues=preview --concurrency=4 --loglevel=info
#
# Required env vars:
#   REDIS_URL      — e.g. redis://localhost:6379/0  (default)
#   CELERY_ENABLED — set true in production; if false the Flask routes fall back
#                    to synchronous conversion (no worker needed for local dev)
# ══════════════════════════════════════════════════════════════════════════════

import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "intellidraft",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # Auto-discover tasks in generation/preview_tasks.py
    include=["generation.preview_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Keep task results in Redis for 2 hours (covers any reasonable polling window)
    result_expires=7200,

    # Track STARTED state so the frontend can show "converting…" vs "queued"
    task_track_started=True,

    # Only acknowledge the task after it completes — no silent loss if worker crashes
    task_acks_late=True,

    # One task per worker slot at a time — prevents memory piling under burst load
    worker_prefetch_multiplier=1,

    # Hard limits on LibreOffice conversion time
    task_soft_time_limit=90,   # sends SoftTimeLimitExceeded after 90s (can be caught)
    task_time_limit=120,       # kills the worker process after 120s (unrecoverable hang)

    # Reconnect automatically if Redis restarts
    broker_connection_retry_on_startup=True,
)
