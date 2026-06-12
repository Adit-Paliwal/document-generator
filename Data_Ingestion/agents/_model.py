"""
Shared Agent Model Selection
=============================
Single source of truth for which LLM all IntelliDraft ADK agents use.

Priority (mirrors llm_provider.py used by generation + vision):
  1. Gemini 2.5 Flash (Google Vertex AI / Gemini API)  — PRIMARY
  2. Azure GPT-5 (Azure OpenAI)                        — FALLBACK

Gemini is chosen automatically when ANY of these are present:
  - Data_Ingestion/key.json         (GCP service account — used by Vertex AI)
  - GEMINI_API_KEY env var          (direct Gemini API key)
  - GOOGLE_API_KEY env var          (also accepted by Gemini)
  - GOOGLE_CLOUD_PROJECT env var    (Application Default Credentials)

Azure is used when:
  - MODEL_PROVIDER=azure_gpt5 is explicitly set in .env, OR
  - MODEL_PROVIDER=azure_openai is explicitly set in .env, OR
  - None of the Gemini credential signals above are present

Override the default in Data_Ingestion/.env:
  MODEL_PROVIDER=gemini        → always use Gemini (default)
  MODEL_PROVIDER=azure_gpt5   → always use Azure GPT-5
  MODEL_PROVIDER=azure_openai → always use a custom Azure OpenAI deployment
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
_BASE = Path(__file__).parent.parent.resolve()   # agents/ → Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from dotenv import load_dotenv   # noqa: E402
load_dotenv(dotenv_path=_BASE / ".env", override=False)

os.environ.setdefault("PYTHONUTF8", "1")


def get_agent_model(agent_name: str = "Agent"):
    """
    Return the LLM model for an ADK LlmAgent.

    Returns either:
      - A string  (e.g. "gemini-2.5-flash")     when using Gemini
      - A LiteLlm instance                       when using Azure

    Args:
        agent_name: Used only for the startup print — e.g. "DocParserAgent".
    """
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower()

    # ── Explicit Azure overrides ──────────────────────────────────────────────
    if provider == "azure_gpt5":
        return _azure_gpt5(agent_name)

    if provider == "azure_openai":
        return _azure_openai(agent_name)

    # ── Gemini (default) — check that credentials exist ───────────────────────
    # If no Gemini credentials are found, automatically fall back to Azure GPT-5
    if _gemini_credentials_available():
        return _gemini(agent_name)

    # No Gemini credentials detected — try Azure GPT-5 if credentials exist,
    # otherwise return the default model string so the container can start.
    # On Vertex AI Agent Engine, ADC via the metadata server is always available
    # even when no env var is set, so the returned model string will work at
    # query time.  If truly no credentials exist, the first query will fail with
    # an auth error (much friendlier than a startup crash).
    azure_key  = os.getenv("AZURE_GPT5_OPENAI_API_KEY", "")
    azure_base = os.getenv("AZURE_GPT5_OPENAI_ENDPOINT", "")
    if azure_key and azure_base:
        print(
            f"[{agent_name}] Gemini credentials not found — falling back to Azure GPT-5.\n"
            f"  TIP: Add  GEMINI_API_KEY=<your-key>  to Data_Ingestion/.env\n"
            f"       (free key at https://aistudio.google.com/app/apikey)"
        )
        return _azure_gpt5(agent_name)

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    print(
        f"[{agent_name}] WARNING: No credentials detected at startup.\n"
        f"  → Using '{model}' (ADC will be used on Vertex AI Agent Engine).\n"
        f"  → For local dev set GEMINI_API_KEY in Data_Ingestion/.env."
    )
    return model


# ─────────────────────────────────────────────────────────────────────────────
# Credential check
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_credentials_available() -> bool:
    """Return True if any Gemini/Vertex AI credential signal is present."""
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return True

    # Explicit project env vars (set manually or via .env)
    if os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID"):
        return True

    # GCP-managed runtime environments — Agent Engine, Cloud Run, GCE, GKE, etc.
    # These always have ADC available via the metadata server even if no env var
    # is explicitly set.  We detect them via well-known platform markers.
    _GCP_MARKERS = (
        "CLOUD_ML_PROJECT_ID",   # Vertex AI training / prediction containers
        "K_SERVICE",             # Cloud Run
        "FUNCTION_NAME",         # Cloud Functions (1st gen)
        "K_REVISION",            # Cloud Run (also present in Agent Engine)
        "GAE_APPLICATION",       # App Engine
        "GCE_METADATA_IP",       # set by some GCE images
    )
    if any(os.getenv(m) for m in _GCP_MARKERS):
        return True

    # key.json — GCP service account used by llm_provider.py / Vertex AI
    #
    # NOTE: Windows paths in .env files can be silently corrupted by dotenv's
    # escape-sequence expansion — e.g. \adit → \x07dit (bell char), \n → newline.
    # To guard against this we ALWAYS check the default key.json location
    # (Data_Ingestion/key.json) as a fallback, even when GOOGLE_KEY_JSON_PATH
    # is set but resolves to a path that does not exist.
    gkjp = os.getenv("GOOGLE_KEY_JSON_PATH", "").strip()
    if gkjp and Path(gkjp).exists():
        return True
    # Always fall back to the default location
    return (_BASE / "key.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Provider builders
# ─────────────────────────────────────────────────────────────────────────────

def _gemini(agent_name: str):
    """Return Gemini model string and set GOOGLE_API_KEY if provided."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        os.environ["GOOGLE_API_KEY"] = api_key

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    print(f"[{agent_name}] Model: Gemini -> {model}")
    return model


def _azure_gpt5(agent_name: str):
    """Return a LiteLlm instance for Azure GPT-5."""
    import litellm
    from google.adk.models.lite_llm import LiteLlm

    key     = os.getenv("AZURE_GPT5_OPENAI_API_KEY",      "")
    base    = os.getenv("AZURE_GPT5_OPENAI_ENDPOINT",     "")
    version = os.getenv("AZURE_GPT5_API_VERSION",         "2024-12-01-preview")
    dep     = os.getenv("AZURE_GPT5_MODEL_DEPLOYMENT_ID", "project-pulse-gpt-5")

    if not key or not base:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        print(
            f"[{agent_name}] WARNING: Azure GPT-5 credentials missing.\n"
            f"  AZURE_GPT5_OPENAI_API_KEY / AZURE_GPT5_OPENAI_ENDPOINT not set.\n"
            f"  → Falling back to '{model}'. Set credentials in .env or env_vars."
        )
        return model

    os.environ.update({
        "AZURE_API_KEY":     key,
        "AZURE_API_BASE":    base,
        "AZURE_API_VERSION": version,
    })

    # ── LiteLLM workaround: preview Azure API versions (2024-12-01-preview+) ──
    # can inject `file_id` content blocks that aren't properly set up, causing
    # Azure to reject the request with "Invalid file data: 'file_id'".
    # drop_params=True tells LiteLLM to silently strip any unsupported params
    # before sending the request, which prevents this error.
    litellm.drop_params = True

    model_str = f"azure/{dep}"
    print(f"[{agent_name}] Model: Azure GPT-5 (fallback) -> {model_str}")
    print(f"[{agent_name}] Endpoint: {base}  API-ver: {version}")
    return LiteLlm(
        model       = model_str,
        api_key     = key,
        api_base    = base,
        api_version = version,
        drop_params = True,   # belt-and-suspenders: also pass to completion args
    )


def _azure_openai(agent_name: str):
    """Return a LiteLlm instance for a generic Azure OpenAI deployment."""
    import litellm
    from google.adk.models.lite_llm import LiteLlm

    key     = os.getenv("AZURE_OPENAI_API_KEY",   "")
    base    = os.getenv("AZURE_OPENAI_ENDPOINT",   "")
    version = os.getenv("AZURE_OPENAI_API_VERSION","2024-02-01")
    dep     = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    if not key or not base:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        print(
            f"[{agent_name}] WARNING: Azure OpenAI credentials missing.\n"
            f"  AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT not set.\n"
            f"  → Falling back to '{model}'. Set credentials in .env or env_vars."
        )
        return model

    os.environ.update({
        "AZURE_API_KEY":     key,
        "AZURE_API_BASE":    base,
        "AZURE_API_VERSION": version,
    })

    litellm.drop_params = True

    model_str = f"azure/{dep}"
    print(f"[{agent_name}] Model: Azure OpenAI (fallback) -> {model_str}")
    return LiteLlm(
        model       = model_str,
        api_key     = key,
        api_base    = base,
        api_version = version,
        drop_params = True,
    )
