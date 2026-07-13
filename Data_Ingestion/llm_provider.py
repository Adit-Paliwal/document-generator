"""
Shared LLM Provider — Gemini Vertex AI (GCP)
=============================================

Used by:
  - api/extractor.py            (POST /api/extract-project-data)
  - generation/derive_fields.py (POST /api/projects/{id}/derive-fields)
  - generation/generator.py     (document section generation)
  - parsers/vision_analyzer.py  (image analysis during document upload)

Provider:
  Gemini 2.5 Flash on Google Vertex AI — authenticated via GCP service account
  key.json (Data_Ingestion/key.json). 1M token context window, generous quota.

Logging:
  Every provider attempt is logged at INFO level.
  Gemini failures are logged at ERROR level and re-raised as RuntimeError.

Model versioning (Vertex AI — Gemini):
  CURRENT DEFAULT:  gemini-2.5-flash
    — GA release, 1M token context, strong reasoning, fast inference.
  ALTERNATIVE:      gemini-2.5-pro   (higher quality, slower, higher cost)
  DEPRECATED:       gemini-1.0-pro   (discontinued Feb 2025 — do NOT use)
                    gemini-2.0-flash  (approaching end-of-life — do NOT use as default)

Configuration (all optional — sensible defaults apply):
  GOOGLE_KEY_JSON_PATH            Path to GCP service account JSON key.
                                  Default: Data_Ingestion/key.json
  VERTEX_LOCATION                 Vertex AI region. Default: us-central1
  GEMINI_VERTEX_MODEL             Gemini model ID. Default: gemini-2.5-flash
"""

from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Absolute path to Data_Ingestion/ — used to locate key.json by default
_BASE = Path(__file__).parent.resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def call_with_fallback(
    messages: list[dict],
    *,
    max_tokens: int = 8_000,
    max_completion_tokens: Optional[int] = None,
    timeout: int = 120,
    log_prefix: str = "[LLM]",
    json_mode: bool = False,
) -> tuple[str, str]:
    """
    Call Gemini Vertex AI.

    Args:
        messages:               OpenAI-style message list — e.g. [{"role":"user","content":"..."}]
        max_tokens:             Token budget for response.
        max_completion_tokens:  Ignored (kept for API compatibility). max_tokens is used.
        timeout:                HTTP timeout in seconds.
        log_prefix:             Module identifier prepended to every log line.
                                e.g. "[Extractor]", "[DeriveFields]"
        json_mode:              Force a JSON object response (Vertex
                                response_mime_type=application/json). Use for
                                any call whose output is parsed as JSON —
                                prevents prose/partial replies from Gemini.

    Returns:
        Tuple of (response_text, provider_used)
        where provider_used is "gemini_vertex"

    Raises:
        RuntimeError: if Gemini fails. Message contains the error details.
    """
    # ── Try Gemini (Vertex AI) ──────────────────────────────────────────────
    creds = _load_gemini_credentials(log_prefix)
    if creds is not None:
        try:
            text = _call_gemini(
                messages    = messages,
                creds       = creds,
                max_tokens  = max_tokens,
                timeout     = timeout,
                log_prefix  = log_prefix,
                json_mode   = json_mode,
            )
            return text, "gemini_vertex"
        except Exception as exc:
            gemini_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "%s Gemini Vertex AI failed — %s.",
                log_prefix, gemini_error,
            )
            raise RuntimeError(
                f"Gemini Vertex AI call failed.\n"
                f"  Error: {gemini_error}\n"
                f"Check key.json exists at Data_Ingestion/key.json and is valid."
            ) from exc
    else:
        raise RuntimeError(
            f"Gemini credentials not found.\n"
            f"Place a valid GCP service account key at Data_Ingestion/key.json, "
            f"or set GOOGLE_KEY_JSON_PATH in Data_Ingestion/.env"
        )


def call_vision_with_fallback(
    text_prompt: str,
    base64_data: str,
    mime_type:   str,
    *,
    max_tokens: int = 512,
    timeout:    int = 30,
    log_prefix: str = "[Vision]",
) -> tuple[str, str]:
    """
    Send an image + text prompt to Gemini 2.5 Flash (Vertex AI).

    Args:
        text_prompt:  The instruction / question about the image.
        base64_data:  Raw base64-encoded image bytes (no data URI prefix).
        mime_type:    MIME type string, e.g. "image/png", "image/jpeg".
        max_tokens:   Max output tokens (512 is sufficient for structured JSON).
        timeout:      HTTP timeout in seconds.
        log_prefix:   Module label for log lines.

    Returns:
        Tuple of (response_text, provider_used).

    Raises:
        RuntimeError: if the Gemini call fails.
    """
    messages = [{
        "role": "user",
        "content": [
            {
                "type":      "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
            },
            {
                "type": "text",
                "text": text_prompt,
            },
        ],
    }]
    return call_with_fallback(
        messages   = messages,
        max_tokens = max_tokens,
        timeout    = timeout,
        log_prefix = log_prefix,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Credentials loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_gemini_credentials(log_prefix: str) -> Optional[dict]:
    """
    Load GCP service account credentials from key.json.
    Returns the credentials dict, or None if the file is missing/unreadable.
    The file is expected to be a standard GCP service account JSON export.
    """
    # NOTE: Windows paths in .env files can be silently corrupted by dotenv's
    # escape-sequence expansion — e.g. \adit → \x07dit (bell char), \n → newline.
    # Strategy: try the custom path first; if it doesn't exist, always fall back
    # to the default Data_Ingestion/key.json regardless of what was set in .env.
    path_env = os.getenv("GOOGLE_KEY_JSON_PATH", "").strip()
    candidates: list[Path] = []
    if path_env:
        candidates.append(Path(path_env))
    candidates.append(_BASE / "key.json")   # always try default as final fallback

    key_path: Optional[Path] = None
    for candidate in candidates:
        if candidate.exists():
            key_path = candidate
            break

    if key_path is None:
        logger.debug(
            "%s key.json not found (tried: %s) — Gemini Vertex AI will be skipped.",
            log_prefix,
            ", ".join(str(c) for c in candidates),
        )
        return None

    try:
        creds = json.loads(key_path.read_text(encoding="utf-8"))
        project = creds.get("project_id", "unknown")
        sa_email = creds.get("client_email", "unknown")
        logger.info(
            "%s Loaded Gemini credentials from %s — project=%s service_account=%s",
            log_prefix, key_path, project, sa_email,
        )
        return creds
    except Exception as exc:
        logger.warning(
            "%s Failed to load key.json at %s: %s",
            log_prefix, key_path, exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Gemini Vertex AI call
# ─────────────────────────────────────────────────────────────────────────────

def _call_gemini(
    messages:   list[dict],
    creds:      dict,
    *,
    max_tokens: int,
    timeout:    int,
    log_prefix: str,
    json_mode:  bool = False,
) -> str:
    """
    Call Gemini on Vertex AI via litellm.

    Model name format for litellm:  vertex_ai/<model_id>
    Authentication:                 vertex_credentials (JSON string of service account)
    Token parameter:                max_tokens → mapped to max_output_tokens by litellm

    Stable GA models (as of 2025-06):
      gemini-2.5-flash           — recommended default (fast, GA, 1M context)
      gemini-2.5-pro             — higher quality, slower, higher cost
    Approaching end-of-life (do NOT use as default):
      gemini-2.0-flash           — scheduled for deprecation
      gemini-2.0-flash-001       — pinned version of the above

    Deprecated (do NOT use):
      gemini-1.0-pro             — discontinued February 2025
    """
    import litellm
    litellm.drop_params = True   # drop params unsupported by this model

    from tenacity import (
        retry, stop_after_attempt, wait_exponential_jitter,
        retry_if_exception, before_sleep_log,
    )

    project  = creds.get("project_id", os.getenv("VERTEX_PROJECT", ""))
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    model_id = os.getenv("GEMINI_VERTEX_MODEL", "gemini-2.5-flash")
    model    = f"vertex_ai/{model_id}"

    # ── Transient-error retry ────────────────────────────────────────────────
    # 429 (rate limit), 5xx, timeouts and connection drops are retried with
    # exponential backoff + jitter. Anything else (auth, bad request, safety
    # block) fails immediately — retrying those only burns time and money.
    _TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
    _TRANSIENT_NAMES  = {
        "Timeout", "APITimeoutError", "APIConnectionError",
        "RateLimitError", "InternalServerError", "ServiceUnavailableError",
    }

    def _is_transient(exc: BaseException) -> bool:
        if getattr(exc, "status_code", None) in _TRANSIENT_STATUS:
            return True
        return type(exc).__name__ in _TRANSIENT_NAMES

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=20),
        retry=retry_if_exception(_is_transient),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _completion_with_retry(**kw):
        return litellm.completion(**kw)

    logger.info(
        "%s → Gemini  model=%s  project=%s  location=%s  max_tokens=%d  timeout=%ds",
        log_prefix, model, project, location, max_tokens, timeout,
    )

    kwargs = {}
    if json_mode:
        # Vertex response_mime_type=application/json — guarantees a JSON body,
        # eliminating the sporadic prose/partial replies seen on long prompts.
        kwargs["response_format"] = {"type": "json_object"}

    t0 = time.time()
    response = _completion_with_retry(
        model              = model,
        messages           = messages,
        vertex_project     = project,
        vertex_location    = location,
        vertex_credentials = json.dumps(creds),   # litellm accepts JSON string
        max_tokens         = max_tokens,           # litellm → max_output_tokens for Vertex
        timeout            = timeout,
        **kwargs,
    )
    elapsed = time.time() - t0

    text  = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    logger.info(
        "%s ✓ Gemini success  elapsed=%.1fs  response_len=%d  usage=%s",
        log_prefix, elapsed, len(text), usage,
    )
    if not text:
        raise ValueError("Gemini returned an empty response")
    return text


