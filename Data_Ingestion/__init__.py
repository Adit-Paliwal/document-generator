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
# DEPLOYMENT: the platform ships as a FastAPI app on Databricks Apps (main.py
# + app.yaml). The Vertex AI Agent Engine deployment path was retired
# 2026-07-13 — these agents run in-process; `adk web` remains for local dev.
#
# DO NOT run from inside Data_Ingestion\ — doubled path breaks ADK discovery.
# ─────────────────────────────────────────────────────────────────────────────
from .agents import root_agent  # noqa: F401
