#!/usr/bin/env python3
"""
deploy_agent_engine.py — Deploy IntelliDraft ADK agents to Vertex AI Agent Engine
====================================================================================

What this does:
  1. Packages the entire Data_Ingestion/ directory (agents + parsers + generation + DB)
  2. Uploads to GCS staging bucket
  3. Deploys as a Vertex AI Agent Engine (ReasoningEngine resource)
  4. Prints the resource name and a test query to verify it's working

Architecture note:
  The ADK agent tools import Python modules DIRECTLY (not via HTTP), so the
  entire Data_Ingestion/ package must be bundled together with the agents.
  The Agent Engine container runs: parsers, storage, db, generation, AND agents
  as one self-contained unit.

  Agent Engine SQLite is separate from Cloud Run SQLite (both LOCAL_DB=true).
  For a shared DB, migrate both to Cloud SQL — a future step.

Prerequisites:
  pip install "google-cloud-aiplatform[reasoningengine]>=1.95.0"
  gcloud auth application-default login
  gcloud services enable aiplatform.googleapis.com storage.googleapis.com

  A GCS bucket for staging:
    gcloud storage buckets create gs://YOUR-BUCKET --location=us-central1

Usage (recommended — ADC, no API key needed):
  gcloud auth login
  gcloud auth application-default login
  python deploy_agent_engine.py \\
      --project=your-gcp-project \\
      --region=asia-south1 \\
      --bucket=your-staging-bucket

  # Or with an explicit Gemini API key (optional):
  python deploy_agent_engine.py \\
      --project=your-gcp-project \\
      --region=asia-south1 \\
      --bucket=your-staging-bucket \\
      --gemini-api-key=AIzaXXXXXXXX

  # Or set env vars and run:
  export GCP_PROJECT_ID=your-project
  export GCS_STAGING_BUCKET=your-bucket
  python deploy_agent_engine.py

  # To update an existing deployment instead of creating a new one:
  python deploy_agent_engine.py --update --resource-id=123456789
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

# ── Repo root (parent of Data_Ingestion/) ────────────────────────────────────
REPO_ROOT = Path(__file__).parent.resolve()
DATA_INGESTION = REPO_ROOT / "Data_Ingestion"

# ── Requirements for Agent Engine (Linux — no pywin32) ───────────────────────
def _load_requirements() -> list[str]:
    """Read requirements.txt and strip Windows-only + comment lines."""
    req_path = DATA_INGESTION / "requirements.txt"
    reqs = []
    for line in req_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip inline comments
        pkg = stripped.split("#")[0].strip()
        if not pkg:
            continue
        # Skip Windows-only packages
        if "pywin32" in pkg.lower():
            continue
        reqs.append(pkg)
    return reqs


# ── CLI args ─────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Deploy IntelliDraft ADK agents to Vertex AI Agent Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # First deploy:
              python deploy_agent_engine.py \\
                  --project=my-project --region=us-central1 \\
                  --bucket=my-staging-bucket --gemini-api-key=AIza...

              # Update existing:
              python deploy_agent_engine.py \\
                  --project=my-project --region=us-central1 \\
                  --bucket=my-staging-bucket --gemini-api-key=AIza... \\
                  --update --resource-id=<id-from-previous-deploy>
        """),
    )
    p.add_argument("--project",        default=os.getenv("GCP_PROJECT_ID"),      help="GCP project ID")
    p.add_argument("--region",         default=os.getenv("GCP_REGION", "asia-south1"), help="GCP region")
    p.add_argument("--bucket",         default=os.getenv("GCS_STAGING_BUCKET"),  help="GCS staging bucket name (no gs:// prefix)")
    p.add_argument("--gemini-api-key", default=os.getenv("GEMINI_API_KEY", ""),  help="Gemini API key (aistudio.google.com/app/apikey)")
    p.add_argument("--display-name",   default="IntelliDraft Document Generator", help="Display name in GCP console")
    p.add_argument("--update",         action="store_true",                       help="Update an existing deployment rather than creating a new one")
    p.add_argument("--resource-id",    default="",                                help="Agent Engine resource ID (required for --update)")
    return p.parse_args()


# ── Main deployment logic ─────────────────────────────────────────────────────

def deploy(args: argparse.Namespace) -> None:

    # Validate required args
    missing = [f"--{k}" for k, v in {
        "project": args.project,
        "bucket":  args.bucket,
    }.items() if not v]
    if missing:
        print(f"\n✗  Missing required arguments: {', '.join(missing)}")
        print("   Set them as CLI flags or env vars (GCP_PROJECT_ID, GCS_STAGING_BUCKET).\n")
        sys.exit(1)

    if args.update and not args.resource_id:
        print("\n✗  --update requires --resource-id  (the numeric ID from the previous deploy)\n")
        sys.exit(1)

    gemini_key = args.gemini_api_key

    # ── Banner ────────────────────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   IntelliDraft — Agent Engine Deployment             ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  Project  : {args.project}")
    print(f"  Region   : {args.region}")
    print(f"  Bucket   : gs://{args.bucket}")
    print(f"  Gemini   : {'API key provided' if gemini_key else '✓ using Application Default Credentials (ADC)'}")
    print(f"  Mode     : {'UPDATE resource ' + args.resource_id if args.update else 'CREATE new deployment'}")
    print()

    # ── Step 1: Set env vars BEFORE importing the agent ──────────────────────
    # These are also passed as runtime env_vars to Agent Engine below.
    print("▶ Step 1/4 — Configuring runtime environment …")

    runtime_env: dict[str, str] = {
        "MODEL_PROVIDER":      "gemini",
        "LOCAL_MODE":          "true",
        "LOCAL_DB":            "true",
        # /tmp is always writable in containers — avoids OneDrive path issues
        "INTELLIDRAFT_DB_DIR": "/tmp/intellidraft",
        # Agent Engine processes one request at a time — no background threads
        "ASYNC_GENERATION":    "false",
        "VISION_ENABLED":      "true",
        "PYTHONUTF8":          "1",
        "GOOGLE_CLOUD_PROJECT": args.project,
        "VERTEX_LOCATION":     args.region,
        "GEMINI_VERTEX_MODEL": "gemini-2.5-flash",
    }
    if gemini_key:
        runtime_env["GEMINI_API_KEY"] = gemini_key

    # Apply locally so module-level code (dotenv, credential checks) works now
    for k, v in runtime_env.items():
        os.environ.setdefault(k, v)

    print("  ✓ Runtime env configured")

    # ── Step 2: Import the root agent ─────────────────────────────────────────
    print("▶ Step 2/4 — Importing root_agent …")
    sys.path.insert(0, str(REPO_ROOT))

    try:
        from Data_Ingestion.agents.orchestrator import root_agent  # noqa: E402
        print(f"  ✓ root_agent loaded: {root_agent.name!r}")
    except Exception as exc:
        print(f"\n✗  Failed to import root_agent: {exc}")
        print("   Make sure you're running from the repo root (Intellidraft/ directory)")
        print("   and that  pip install -r Data_Ingestion/requirements.txt  has been run.\n")
        raise

    # ── Step 3: Initialise Vertex AI ──────────────────────────────────────────
    print("▶ Step 3/4 — Connecting to Vertex AI …")
    import vertexai
    from vertexai.preview import reasoning_engines

    vertexai.init(
        project        = args.project,
        location       = args.region,
        staging_bucket = f"gs://{args.bucket}",
    )
    print("  ✓ Vertex AI initialised")

    # ── Step 4: Deploy ────────────────────────────────────────────────────────
    requirements = _load_requirements()
    print(f"▶ Step 4/4 — {'Updating' if args.update else 'Creating'} Agent Engine …")
    print(f"  Packaging {DATA_INGESTION.name}/ ({len(requirements)} requirements) …")
    print("  (first deploy takes ~5-10 min to upload and build)\n")

    # Wrap the ADK agent in an AdkApp for Agent Engine
    app = reasoning_engines.AdkApp(
        agent          = root_agent,
        enable_tracing = True,
    )

    deploy_kwargs: dict = {
        "requirements":   requirements,
        "extra_packages": [str(DATA_INGESTION)],  # bundles entire Data_Ingestion/
        "display_name":   args.display_name,
        "description": (
            "IntelliDraft multi-agent document generation system. "
            "Orchestrates DocParserAgent, ContextCollectorAgent, and "
            "DocumentGeneratorAgent to parse documents, load project context, "
            "and generate/modify BRD, RFP, SOW, Proposals, and Tech Specs."
        ),
    }

    # env_vars supported in google-cloud-aiplatform >= 1.95 — try gracefully
    try:
        if args.update:
            remote = reasoning_engines.ReasoningEngine(
                f"projects/{args.project}/locations/{args.region}"
                f"/reasoningEngines/{args.resource_id}"
            )
            remote.update(
                reasoning_engine = app,
                env_vars         = runtime_env,
                **{k: v for k, v in deploy_kwargs.items() if k != "display_name"},
            )
        else:
            remote = reasoning_engines.ReasoningEngine.create(
                app,
                env_vars = runtime_env,
                **deploy_kwargs,
            )
    except TypeError:
        # Older SDK — env_vars not supported as a parameter
        print("  ⚠  env_vars param not supported by this SDK version.")
        print("     Deploying without — you'll need to set them via gcloud after.")
        print("     Upgrade: pip install 'google-cloud-aiplatform[reasoningengine]>=1.95.0'\n")
        if args.update:
            remote = reasoning_engines.ReasoningEngine(
                f"projects/{args.project}/locations/{args.region}"
                f"/reasoningEngines/{args.resource_id}"
            )
            remote.update(reasoning_engine=app, **{k: v for k, v in deploy_kwargs.items() if k != "display_name"})
        else:
            remote = reasoning_engines.ReasoningEngine.create(app, **deploy_kwargs)

        # Print gcloud workaround for env vars
        print("\n  To set env vars manually (run after this script finishes):")
        resource_id = remote.name.split("/")[-1]
        for k, v in runtime_env.items():
            if k == "GEMINI_API_KEY":
                continue  # handle secrets separately
            print(f"    gcloud beta ai agent-engines update {resource_id}"
                  f" --region={args.region} --project={args.project}"
                  f" --update-env-vars {k}={v}")
        if gemini_key:
            print(f"\n  For GEMINI_API_KEY, use Secret Manager:")
            print(f"    gcloud secrets create intellidraft-gemini-key --data-file=- <<< '{gemini_key[:8]}...'")
            print(f"    # Then mount it as an env var in the Agent Engine resource config")

    # ── Done ──────────────────────────────────────────────────────────────────
    resource_name = remote.resource_name
    resource_id   = resource_name.split("/")[-1]

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   ✅  Agent Engine deployment complete!              ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print(f"  Resource name : {resource_name}")
    print(f"  Resource ID   : {resource_id}")
    print()
    print("  ── How to query the agent ────────────────────────────")
    print("  Python:")
    print(f"    import vertexai")
    print(f"    from vertexai.preview import reasoning_engines")
    print(f"    vertexai.init(project='{args.project}', location='{args.region}')")
    print(f"    agent = reasoning_engines.ReasoningEngine('{resource_name}')")
    print(f"    session = agent.create_session(user_id='user1')")
    print(f"    response = agent.stream_query(")
    print(f"        user_id='user1',")
    print(f"        session_id=session['id'],")
    print(f"        message='Hello! What can you help me with?'")
    print(f"    )")
    print(f"    for chunk in response:")
    print(f"        print(chunk, end='', flush=True)")
    print()
    print("  ── ADK CLI (quick test) ──────────────────────────────")
    print(f"  adk deploy agent_engine test \\")
    print(f"      --project={args.project} --region={args.region} \\")
    print(f"      --resource_id={resource_id} \\")
    print(f"      --message='Hello!'")
    print()
    print("  ── GCP Console ───────────────────────────────────────")
    print(f"  https://console.cloud.google.com/vertex-ai/agents/")
    print(f"  (Project: {args.project}, Region: {args.region})")
    print()
    print("  Save the Resource ID for future updates:")
    print(f"    python deploy_agent_engine.py --update --resource-id={resource_id} ...")
    print()
    print("  ⚠  NOTE: LOCAL_DB=true → SQLite is in /tmp — session data is")
    print("     per-container-instance and not shared with Cloud Run.")
    print("     For production: migrate to Cloud SQL and set LOCAL_DB=false.")
    print()


if __name__ == "__main__":
    deploy(_parse_args())
