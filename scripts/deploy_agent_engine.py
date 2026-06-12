#!/usr/bin/env python3
"""
Deploy IntelliDraft to Vertex AI Agent Engine.

Usage (from the repo root):
  python scripts/deploy_agent_engine.py \
      --project  my-gcp-project          \
      --region   us-central1             \
      --display-name "IntelliDraft"

Minimum requirements:
  google-cloud-aiplatform >= 1.74.0   (env_vars support)
  google-adk >= 1.0.0
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
from pathlib import Path


# ── 0. Version gate — fail early with a clear message ────────────────────────

def _require_sdk_version(pkg: str, minimum: tuple[int, ...]) -> None:
    try:
        raw = importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        print(f"ERROR: {pkg} is not installed.  Run: pip install '{pkg}>={'.'.join(map(str, minimum))}'")
        sys.exit(1)

    parsed = tuple(int(x) for x in raw.split(".")[:len(minimum)] if x.isdigit())
    if parsed < minimum:
        min_str = ".".join(map(str, minimum))
        print(
            f"ERROR: {pkg} {raw} is installed, but >= {min_str} is required.\n"
            f"       The env_vars parameter was added in {min_str}.\n"
            f"       Fix:  pip install --upgrade '{pkg}>={min_str}'"
        )
        sys.exit(1)

    print(f"  {pkg} {raw}  OK")


# ── 1. Bootstrap: add repo root and Data_Ingestion to sys.path ───────────────

_REPO_ROOT = Path(__file__).parent.parent.resolve()
_DATA_INGESTION = _REPO_ROOT / "Data_Ingestion"

for _p in (_REPO_ROOT, _DATA_INGESTION):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── 2. Parse arguments ────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Deploy IntelliDraft to Vertex AI Agent Engine."
    )
    p.add_argument("--project",      required=True,  help="GCP project ID")
    p.add_argument("--region",       default="us-central1", help="GCP region (default: us-central1)")
    p.add_argument("--display-name", default="IntelliDraft Document Generator",
                   help="Display name shown in the GCP console")
    p.add_argument("--env-file",     default=str(_DATA_INGESTION / ".env"),
                   help="Path to .env file to source for runtime env vars")
    p.add_argument("--staging-bucket", default="",
                   help="GCS staging bucket (gs://bucket-name). Uses project default if omitted.")
    return p.parse_args()


# ── 3. Load .env file into os.environ (local only — NOT deployed) ─────────────

def _load_env_file(env_file: str) -> None:
    path = Path(env_file)
    if not path.exists():
        print(f"  WARNING: .env file not found at {path}  (skipping)")
        return
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=path, override=False)
    print(f"  Loaded env vars from {path}")


# ── 4. Collect runtime env vars to embed in the deployed agent ────────────────

def _collect_env_vars() -> dict[str, str]:
    """
    Build the dict of env vars that will be injected into the Agent Engine
    container at runtime.  Only include vars that are set and non-empty.
    """
    candidates = {
        # Gemini / Vertex AI
        "GOOGLE_API_KEY":              os.getenv("GOOGLE_API_KEY", ""),
        "GEMINI_API_KEY":              os.getenv("GEMINI_API_KEY", ""),
        "GEMINI_MODEL":                os.getenv("GEMINI_MODEL", ""),
        "GOOGLE_CLOUD_PROJECT":        os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        "GCP_PROJECT_ID":              os.getenv("GCP_PROJECT_ID", ""),
        "GOOGLE_CLOUD_LOCATION":       os.getenv("GOOGLE_CLOUD_LOCATION", ""),

        # Azure GPT-5 fallback
        "MODEL_PROVIDER":              os.getenv("MODEL_PROVIDER", ""),
        "AZURE_GPT5_OPENAI_API_KEY":   os.getenv("AZURE_GPT5_OPENAI_API_KEY", ""),
        "AZURE_GPT5_OPENAI_ENDPOINT":  os.getenv("AZURE_GPT5_OPENAI_ENDPOINT", ""),
        "AZURE_GPT5_API_VERSION":      os.getenv("AZURE_GPT5_API_VERSION", ""),
        "AZURE_GPT5_MODEL_DEPLOYMENT_ID": os.getenv("AZURE_GPT5_MODEL_DEPLOYMENT_ID", ""),

        # Azure OpenAI generic fallback
        "AZURE_OPENAI_API_KEY":        os.getenv("AZURE_OPENAI_API_KEY", ""),
        "AZURE_OPENAI_ENDPOINT":       os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        "AZURE_OPENAI_API_VERSION":    os.getenv("AZURE_OPENAI_API_VERSION", ""),
        "AZURE_OPENAI_DEPLOYMENT":     os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),

        # Azure storage / Cosmos
        "AZURE_STORAGE_CONNECTION_STRING": os.getenv("AZURE_STORAGE_CONNECTION_STRING", ""),
        "AZURE_STORAGE_CONTAINER":     os.getenv("AZURE_STORAGE_CONTAINER", ""),
        "COSMOS_ENDPOINT":             os.getenv("COSMOS_ENDPOINT", ""),
        "COSMOS_KEY":                  os.getenv("COSMOS_KEY", ""),
        "COSMOS_DATABASE":             os.getenv("COSMOS_DATABASE", ""),
        "COSMOS_CONTAINER":            os.getenv("COSMOS_CONTAINER", ""),

        # Misc
        "PYTHONUTF8":                  "1",
    }
    return {k: v for k, v in candidates.items() if v}


# ── 5. Deploy ─────────────────────────────────────────────────────────────────

def deploy(args: argparse.Namespace) -> None:
    print("\n  ╔══════════════════════════════════════════╗")
    print("  ║  IntelliDraft — Agent Engine Deploy      ║")
    print("  ╚══════════════════════════════════════════╝\n")

    # --- Version gate ---
    print("Checking SDK versions...")
    _require_sdk_version("google-cloud-aiplatform", (1, 74, 0))

    # --- Load .env ---
    print("\nLoading environment variables...")
    _load_env_file(args.env_file)
    env_vars = _collect_env_vars()
    if not env_vars:
        print(
            "  WARNING: No runtime env vars found. The deployed agent may fail\n"
            "           to start if it needs API keys. Set them in Data_Ingestion/.env."
        )
    else:
        print(f"  Runtime env vars to embed: {sorted(env_vars.keys())}")

    # --- Init Vertex AI ---
    print(f"\nInitialising Vertex AI  (project={args.project}, region={args.region})...")
    import vertexai
    init_kwargs: dict = {"project": args.project, "location": args.region}
    if args.staging_bucket:
        init_kwargs["staging_bucket"] = args.staging_bucket
    vertexai.init(**init_kwargs)

    # --- Import agent ---
    print("\nLoading root agent...")
    from Data_Ingestion.agents.orchestrator import root_agent  # noqa: E402

    # --- Wrap in AdkApp ---
    # Try the GA path first (google-cloud-aiplatform >= 1.87), then preview.
    print("Wrapping agent in AdkApp...")
    try:
        from vertexai.agent_engines import AdkApp
        from vertexai import agent_engines as _ae_mod
        _create_fn = _ae_mod.AgentEngine.create
        _create_kwargs_key = "agent_engine"
        print("  Using vertexai.agent_engines (GA)")
    except ImportError:
        from vertexai.preview.reasoning_engines import AdkApp  # type: ignore[no-redef]
        from vertexai.preview import reasoning_engines as _re_mod
        _create_fn = _re_mod.ReasoningEngine.create
        _create_kwargs_key = "reasoning_engine"
        print("  Using vertexai.preview.reasoning_engines")

    app = AdkApp(agent=root_agent, enable_tracing=False)

    # --- Requirements to install on the remote container ---
    adk_ver   = importlib.metadata.version("google-adk")
    aipt_ver  = importlib.metadata.version("google-cloud-aiplatform")
    requirements = [
        f"google-adk=={adk_ver}",
        f"google-cloud-aiplatform=={aipt_ver}",
        "python-dotenv>=1.0.1",
        "litellm>=1.83.0",
        "openai>=1.0.0",
        "google-genai>=1.0.0",
        "PyMuPDF>=1.24.0",
        "python-docx>=1.1.0",
        "python-pptx>=1.0.0",
        "openpyxl>=3.1.0",
        "SQLAlchemy>=2.0.0",
        "aiosqlite>=0.20.0",
        "azure-storage-blob>=12.0.0",
        "azure-cosmos>=4.0.0",
        "azure-identity>=1.15.0",
        "pydantic>=2.0.0",
        "cloudpickle>=3.0.0",
    ]

    # --- Create ---
    print(f"\nDeploying '{args.display_name}' to Agent Engine ...")
    print("  (first deploy takes 5-10 minutes to package and build)\n")

    create_kwargs = {
        _create_kwargs_key: app,
        "requirements":     requirements,
        "display_name":     args.display_name,
        "env_vars":         env_vars,
    }

    remote = _create_fn(**create_kwargs)

    print("\n  ✅  Deployment complete!")
    print(f"  Resource name : {remote.resource_name}")
    print(f"  Console URL   : https://console.cloud.google.com/vertex-ai/agents")
    print(f"\n  To query the agent:")
    print(f'    session = remote.create_session(user_id="test")')
    print(f'    for ev in remote.stream_query(user_id="test", session_id=session["id"], message="Hello"):')
    print(f'        print(ev)')
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    deploy(_parse_args())
