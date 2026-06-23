"""
preview_tasks.py
================
Celery task definitions for LibreOffice document-to-HTML conversion.

This module is auto-discovered by celery_app.py via the `include` config.
The task delegates all logic to preview_service.py — this file only provides
the Celery task decorator and retry/error-handling wrapper.
"""
from __future__ import annotations

import logging

from celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="preview.convert_docx",
    bind=True,
    max_retries=2,
    default_retry_delay=5,   # seconds between retries
    acks_late=True,          # confirm only after success (no silent drop on worker crash)
)
def convert_docx_task(self, job_id: str) -> str:
    """
    Convert a generation job's DOCX to a self-contained HTML string.

    Args:
        job_id: The generation job UUID.

    Returns:
        The full HTML string (CSS and images inlined).

    Raises:
        Retries up to max_retries times on failure, then propagates the exception
        (which marks the Celery task as FAILURE so the frontend can surface it).
    """
    from generation.preview_service import convert_job_to_html, _cache_set

    logger.info("[preview task] Starting conversion for job %s (attempt %d)", job_id, self.request.retries + 1)
    try:
        html = convert_job_to_html(job_id)
        _cache_set(job_id, html)
        logger.info("[preview task] Completed conversion for job %s (%d bytes)", job_id, len(html))
        return html
    except Exception as exc:
        logger.warning("[preview task] Conversion failed for job %s: %s", job_id, exc)
        raise self.retry(exc=exc)
