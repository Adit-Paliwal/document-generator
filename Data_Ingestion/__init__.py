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
# Pattern (c) also works via agent/__init__.py.
#
# HOW TO RUN:
#   cd "…\Intellidraft"        ← the PARENT of Data_Ingestion\
#   adk web                    ← agents_dir defaults to the current directory
#
# DO NOT run from inside Data_Ingestion\ — that creates a doubled path and
# ADK cannot find the agent.
# ─────────────────────────────────────────────────────────────────────────────
from .agent import root_agent  # noqa: F401
