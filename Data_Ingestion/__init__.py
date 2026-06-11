# ─────────────────────────────────────────────────────────────────────────────
# ADK agent-package entry point
# ─────────────────────────────────────────────────────────────────────────────
# Google ADK (v2) discovers root_agent by trying three patterns:
#
#   (b) import Data_Ingestion          → root_agent on the package itself  ← THIS FILE
#   (c) import Data_Ingestion.agent    → root_agent on the agent sub-module
#   (d) Data_Ingestion/root_agent.yaml
#
# This __init__.py covers pattern (b) — the most direct path.
#
# MULTI-AGENT ARCHITECTURE:
#   root_agent = IntelliDraftOrchestrator (LlmAgent)
#     ├── DocParserAgent          (agents/doc_parser/)
#     ├── ContextCollectorAgent   (agents/context_collector/)
#     └── DocumentGeneratorAgent  (agents/document_generator/)
#
# HOW TO RUN (local):
#   cd "…\Intellidraft"        ← the PARENT of Data_Ingestion\
#   adk web                    ← discovers Data_Ingestion as the agent app
#
# HOW TO DEPLOY (Agent Engine):
#   adk deploy agent_engine \
#     --project=$GCP_PROJECT_ID \
#     --region=us-central1 \
#     --display_name="IntelliDraft Document Generator" \
#     Data_Ingestion
#
# DO NOT run from inside Data_Ingestion\ — doubled path breaks ADK discovery.
#
# LEGACY: agents/doc_parser/ replaces the old single-agent at agent/agent.py.
#         agent/agent.py is kept for backward-compat single-agent testing.
# ─────────────────────────────────────────────────────────────────────────────
from .agents import root_agent  # noqa: F401
