"""
IntelliDraft Orchestrator
==========================
Top-level LlmAgent that routes user requests to the correct specialist.

Sub-agents  (all live under agents/ — one place, easy to find):
  agents/doc_parser/          — DocParserAgent   (file upload, parsing, Vision AI)
  agents/context_collector/   — ContextCollectorAgent  (load project from DB)
  agents/document_generator/  — DocumentGeneratorAgent (generate + modify + export)
  agents/reviewer/            — ReviewerAgent    (share for review, comments, AI
                                 persona reviews, feedback summaries, approvals)

Routing (ADK reads each sub-agent's `description` to decide):
  "parse / upload / extract content from file"    → DocParserAgent
  "project context / load project / check fields" → ContextCollectorAgent
  "generate / modify section / export document"   → DocumentGeneratorAgent
  "share / review / comments / approve / summarize feedback" → ReviewerAgent

Session state shared across all sub-agents via InvocationContext:
  parsed_document_ids      — set by DocParserAgent after each successful parse
  last_parsed_document_id  — most recently parsed document ID
  project_id               — set by ContextCollectorAgent after loading context
  project_context          — full ingested + derived context dict
  document_ids             — list of doc IDs attached to the project
  document_type            — e.g. "BRD"
  job_id                   — set by DocumentGeneratorAgent after start_generation

Deployment:
  adk web                           — local dev  (run from Intellidraft/ parent dir)
  adk deploy agent_engine ...       — Vertex AI Agent Engine
"""

from __future__ import annotations
import sys
from pathlib import Path

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
_BASE = Path(__file__).parent.parent.resolve()   # → …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from google.adk.agents import LlmAgent
from ._model           import get_agent_model

# ── All four specialist agents live under agents/ ────────────────────────────
from .doc_parser.agent         import doc_parser_agent
from .context_collector.agent  import context_collector_agent
from .document_generator.agent import document_generator_agent
from .reviewer.agent           import reviewer_agent

# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator instruction
# ─────────────────────────────────────────────────────────────────────────────

_INSTRUCTION = """
You are IntelliDraft, an AI assistant for generating professional business
documents (BRD, RFP, SOW, Project Proposals, Technical Specifications).

You coordinate three specialist agents. Route every user request to the
correct specialist — do NOT handle their tasks yourself.

SPECIALIST AGENTS
-----------------
1. doc_processor  (Document Parser)
   → When: user uploads a file, asks to parse a document, or asks about
     document contents (text, tables, diagrams, workflows).
   → Handles: PDF, DOCX, PPTX, XLSX parsing with Vision AI for diagrams.
   → After parsing, the document_id is saved automatically in session state.

2. ContextCollectorAgent
   → When: user provides a project_id, wants to review their project details,
     check if required fields are complete, or see what will be used for
     generation.
   → Handles: loading ingested (form-filled) + derived (AI-extracted) project
     fields from the database. Validates readiness for generation.

3. DocumentGeneratorAgent
   → When: user wants to generate a document, modify a section,
     review generated content, or export the final document.
   → Handles: section-by-section generation, chatbot modifications
     (only the requested section is regenerated — never the full document),
     and export (Word, PDF, Markdown).

4. ReviewerAgent
   → When: user wants to share a document for review, check reviewer statuses,
     add or discuss review comments, run an AI persona review, summarize
     reviewer feedback, approve/reject a document, or apply review feedback
     to a section.
   → Handles: the full post-generation review lifecycle, including AI persona
     reviews and persona-wise feedback summaries.

TYPICAL FLOW
------------
  Step 1  User uploads source document       → route to doc_processor
  Step 2  (User fills/edits form in frontend — not via chat)
  Step 3  User provides project_id           → route to ContextCollectorAgent
  Step 4  User says "generate my BRD"        → route to DocumentGeneratorAgent
  Step 5  User requests section changes      → DocumentGeneratorAgent handles
  Step 6  User shares document for review    → ReviewerAgent handles
  Step 7  Reviewers comment / approve        → ReviewerAgent handles
  Step 8  Author applies review feedback     → ReviewerAgent (regenerates the
          targeted section via the standard flow)

RULES
-----
  - Greet the user warmly on the first message and explain the flow above.
  - If you are unsure which agent to use, ask the user to clarify.
  - Session state (project_id, job_id, parsed_document_ids) persists across the
    conversation — do not ask for values already stored in state.
  - Always route to ContextCollectorAgent to load context BEFORE starting
    generation, unless job_id is already in session state.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Root agent — discovered by ADK via Data_Ingestion/__init__.py → agents/__init__.py
# ─────────────────────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    name        = "IntelliDraftOrchestrator",
    model       = get_agent_model("Orchestrator"),
    description = (
        "IntelliDraft orchestrator. Routes document-parsing, project-context, "
        "and document-generation requests to the appropriate specialist agent."
    ),
    instruction = _INSTRUCTION,
    sub_agents  = [
        doc_parser_agent,        # agents/doc_parser/agent.py
        context_collector_agent, # agents/context_collector/agent.py
        document_generator_agent,# agents/document_generator/agent.py
        reviewer_agent,          # agents/reviewer/agent.py
    ],
)
