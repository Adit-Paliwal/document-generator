"""
Shared Agent Model Selection
=============================
Single source of truth for which LLM all IntelliDraft ADK agents use.

Provider: Gemini 2.5 Flash (Google Vertex AI / Gemini API)

Gemini is chosen automatically when ANY of these are present:
  - Data_Ingestion/key.json         (GCP service account — used by Vertex AI)
  - GEMINI_API_KEY env var          (direct Gemini API key)
  - GOOGLE_API_KEY env var          (also accepted by Gemini)
  - GOOGLE_CLOUD_PROJECT env var    (Application Default Credentials)

Override the model in Data_Ingestion/.env:
  GEMINI_MODEL=gemini-2.5-flash     (default)
  GEMINI_MODEL=gemini-2.5-pro       (higher quality, slower, higher cost)
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
    Return the Gemini model string for an ADK LlmAgent.

    Args:
        agent_name: Used only for the startup print — e.g. "DocParserAgent".
    """
    if not _gemini_credentials_available():
        raise RuntimeError(
            f"[{agent_name}] Gemini credentials not found.\n"
            f"  Place a valid GCP service account key at Data_Ingestion/key.json,\n"
            f"  or set GEMINI_API_KEY in Data_Ingestion/.env"
        )
    return _gemini(agent_name)


# ─────────────────────────────────────────────────────────────────────────────
# Credential check
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_credentials_available() -> bool:
    """Return True if any Gemini/Vertex AI credential signal is present."""
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return True
    if os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID"):
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


