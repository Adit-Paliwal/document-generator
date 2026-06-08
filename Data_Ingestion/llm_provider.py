"""
Shared LLM Provider — Gemini Vertex AI (primary) → Azure GPT-5 (fallback)
==========================================================================

Used by:
  - api/extractor.py            (POST /api/extract-project-data)
  - generation/derive_fields.py (POST /api/projects/{id}/derive-fields)
  - generation/generator.py     (document section generation)

Provider order:
  1. Gemini 2.5 Flash on Google Vertex AI — authenticated via GCP service account
     key.json (Data_Ingestion/key.json). 1M token context window, generous quota,
     no network firewall restrictions. Used as the primary provider.
  2. Azure GPT-5 — automatic fallback if Gemini fails for any reason
     (key.json missing, Vertex AI unreachable, quota exhausted, API error).
     Azure credentials are read from .env as before.

Logging:
  Every provider attempt is logged at INFO level.
  Gemini failures are logged at WARNING level (triggering fallback).
  Azure failures are logged at ERROR level.
  The caller always knows which provider was used (returned in the tuple).

Model versioning (Vertex AI — Gemini):
  CURRENT DEFAULT:  gemini-2.5-flash
    — GA release, 1M token context, strong reasoning, fast inference.
    — Replaces gemini-2.0-flash which is approaching end-of-life.
  ALTERNATIVE:      gemini-2.5-pro   (higher quality, slower, higher cost)
  DEPRECATED:       gemini-1.0-pro   (discontinued Feb 2025 — do NOT use)
                    gemini-2.0-flash  (approaching end-of-life — do NOT use as default)

Configuration (all optional — sensible defaults apply):
  GOOGLE_KEY_JSON_PATH            Path to GCP service account JSON key.
                                  Default: Data_Ingestion/key.json
  VERTEX_LOCATION                 Vertex AI region. Default: us-central1
  GEMINI_VERTEX_MODEL             Gemini model ID. Default: gemini-2.5-flash

  AZURE_GPT5_OPENAI_API_KEY       Azure OpenAI API key
  AZURE_GPT5_OPENAI_ENDPOINT      Azure OpenAI endpoint URL
  AZURE_GPT5_API_VERSION          API version (default: 2024-12-01-preview)
  AZURE_GPT5_MODEL_DEPLOYMENT_ID  Azure deployment name (default: gpt-5)
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
) -> tuple[str, str]:
    """
    Call Gemini Vertex AI first; automatically fall back to Azure GPT-5 on failure.

    Args:
        messages:               OpenAI-style message list — e.g. [{"role":"user","content":"..."}]
        max_tokens:             Token budget for response (Gemini + standard Azure).
                                Maps to max_output_tokens for Gemini, max_tokens for Azure GPT-4.
        max_completion_tokens:  Token budget specifically for GPT-5 reasoning model (Azure).
                                If None, max_tokens is used for both.
        timeout:                HTTP timeout in seconds (applies to both providers).
        log_prefix:             Module identifier prepended to every log line.
                                e.g. "[Extractor]", "[DeriveFields]"

    Returns:
        Tuple of (response_text, provider_used)
        where provider_used is "gemini_vertex" or "azure_gpt5"

    Raises:
        RuntimeError: if ALL providers fail. Message contains both error details.
    """
    gemini_error: Optional[str] = None
    azure_error:  Optional[str] = None

    # ── 1. Try Gemini (Vertex AI) ──────────────────────────────────────────────
    creds = _load_gemini_credentials(log_prefix)
    if creds is not None:
        try:
            text = _call_gemini(
                messages    = messages,
                creds       = creds,
                max_tokens  = max_tokens,
                timeout     = timeout,
                log_prefix  = log_prefix,
            )
            return text, "gemini_vertex"
        except Exception as exc:
            gemini_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "%s Gemini Vertex AI failed — %s. Falling back to Azure GPT-5.",
                log_prefix, gemini_error,
            )
    else:
        gemini_error = "key.json not found or unreadable"
        logger.info(
            "%s Gemini credentials unavailable (%s) — using Azure GPT-5.",
            log_prefix, gemini_error,
        )

    # ── 2. Fallback: Azure GPT-5 ───────────────────────────────────────────────
    azure_tokens = max_completion_tokens if max_completion_tokens is not None else max_tokens
    try:
        text = _call_azure_gpt5(
            messages              = messages,
            max_completion_tokens = azure_tokens,
            timeout               = timeout,
            log_prefix            = log_prefix,
        )
        return text, "azure_gpt5"
    except Exception as exc:
        azure_error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "%s Azure GPT-5 fallback also failed — %s.",
            log_prefix, azure_error,
        )

    # ── Both failed ───────────────────────────────────────────────────────────
    raise RuntimeError(
        f"All LLM providers failed.\n"
        f"  Gemini: {gemini_error}\n"
        f"  Azure GPT-5: {azure_error}\n"
        f"Check key.json exists and Azure env vars (AZURE_GPT5_OPENAI_API_KEY, "
        f"AZURE_GPT5_OPENAI_ENDPOINT) are set correctly in Data_Ingestion/.env"
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
    path_env = os.getenv("GOOGLE_KEY_JSON_PATH", "").strip()
    key_path = Path(path_env) if path_env else (_BASE / "key.json")

    if not key_path.exists():
        logger.debug(
            "%s key.json not found at %s — Gemini Vertex AI will be skipped.",
            log_prefix, key_path,
        )
        return None

    try:
        creds = json.loads(key_path.read_text(encoding="utf-8"))
        project = creds.get("project_id", "unknown")
        sa_email = creds.get("client_email", "unknown")
        logger.info(
            "%s Loaded Gemini credentials — project=%s service_account=%s",
            log_prefix, project, sa_email,
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

    project  = creds.get("project_id", os.getenv("VERTEX_PROJECT", ""))
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    model_id = os.getenv("GEMINI_VERTEX_MODEL", "gemini-2.5-flash")
    model    = f"vertex_ai/{model_id}"

    logger.info(
        "%s → Gemini  model=%s  project=%s  location=%s  max_tokens=%d  timeout=%ds",
        log_prefix, model, project, location, max_tokens, timeout,
    )

    t0 = time.time()
    response = litellm.completion(
        model              = model,
        messages           = messages,
        vertex_project     = project,
        vertex_location    = location,
        vertex_credentials = json.dumps(creds),   # litellm accepts JSON string
        max_tokens         = max_tokens,           # litellm → max_output_tokens for Vertex
        timeout            = timeout,
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


# ─────────────────────────────────────────────────────────────────────────────
# Azure GPT-5 call (fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _call_azure_gpt5(
    messages:              list[dict],
    *,
    max_completion_tokens: int,
    timeout:               int,
    log_prefix:            str,
) -> str:
    """
    Call Azure GPT-5 via litellm.

    GPT-5 is a reasoning model — key differences vs GPT-4:
      - Does NOT accept temperature parameter (drop_params handles this)
      - Uses max_completion_tokens (not max_tokens) for output length
      - System + user are merged into a single user message to avoid the
        "system message ignored" bug observed with reasoning models in litellm
    """
    import litellm
    litellm.drop_params = True   # GPT-5 doesn't support temperature, top_p etc.

    key     = os.getenv("AZURE_GPT5_OPENAI_API_KEY",      "")
    base    = os.getenv("AZURE_GPT5_OPENAI_ENDPOINT",     "")
    version = os.getenv("AZURE_GPT5_API_VERSION",         "2024-12-01-preview")
    dep     = os.getenv("AZURE_GPT5_MODEL_DEPLOYMENT_ID", "gpt-5")
    model   = f"azure/{dep}"

    if not key or not base:
        raise EnvironmentError(
            "Azure GPT-5 credentials not configured. "
            "Set AZURE_GPT5_OPENAI_API_KEY and AZURE_GPT5_OPENAI_ENDPOINT in .env"
        )

    # litellm reads Azure credentials from os.environ
    os.environ.update({
        "AZURE_API_KEY":     key,
        "AZURE_API_BASE":    base,
        "AZURE_API_VERSION": version,
    })

    logger.info(
        "%s → Azure GPT-5  model=%s  max_completion_tokens=%d  timeout=%ds",
        log_prefix, model, max_completion_tokens, timeout,
    )

    t0 = time.time()
    response = litellm.completion(
        model                 = model,
        messages              = messages,
        max_completion_tokens = max_completion_tokens,
        timeout               = timeout,
    )
    elapsed = time.time() - t0

    text  = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    logger.info(
        "%s ✓ Azure GPT-5 success  elapsed=%.1fs  response_len=%d  usage=%s",
        log_prefix, elapsed, len(text), usage,
    )
    if not text:
        raise ValueError("Azure GPT-5 returned an empty response")
    return text
