"""
DocumentGeneratorAgent — Agent 3
==================================
The generation chatbot. Generates the document and handles section modifications.

Responsibility:
  • Start generation for a saved project (uses project context from DB)
  • Report generation progress section-by-section
  • Let the user modify individual sections via natural-language chat
  • Only regenerate the section(s) the user asks to change — never the full document
  • Export the final document

Chatbot example:
  User: "Generate my BRD"
  Agent: starts generation, polls status, reports when done

  User: "The Executive Summary is too technical, simplify it"
  Agent: calls modify_section(exec_summary_id, "Simplify for business stakeholders")
         shows ONLY the updated Executive Summary

  User: "Add a risk table to Risk Register"
  Agent: calls modify_section(risk_id, "Add a formatted risk table")

  User: "Export as Word"
  Agent: calls export_document → returns file path

Model: Gemini 2.5 Flash (primary) → Azure GPT-5 (fallback).
       Controlled by agents/_model.py + Data_Ingestion/.env.
"""

from __future__ import annotations
from google.adk.agents import LlmAgent
from .._model          import get_agent_model

from .tools import (
    start_generation,
    get_job_status,
    list_sections,
    get_section_content,
    modify_section,
    export_document,
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent instruction
# ─────────────────────────────────────────────────────────────────────────────

_INSTRUCTION = """
You are the Document Generator specialist for IntelliDraft.

Your job is to generate business documents (BRD, RFP, SOW, Proposals, etc.)
section-by-section and let users refine individual sections via chat.

CRITICAL RULE — SECTION-ONLY MODIFICATION:
  When a user asks to change anything, ONLY regenerate the specific section
  they mentioned using modify_section(). NEVER regenerate the whole document.
  Other sections are never touched unless the user explicitly asks for them.

WORKFLOW:

  PHASE 1 — GENERATE
  ------------------
  1. Ask the user for their project_id if not already known
     (or read it from session state if ContextCollectorAgent already loaded it).
  2. Call start_generation(project_id) to kick off generation.
  3. Poll get_job_status every few seconds — report progress to the user.
     ("3 of 8 sections done…", "All 8 sections complete!")
  4. Once status = "completed", call list_sections to show all sections
     with titles and word counts. Invite the user to review them.

  PHASE 2 — REVIEW AND MODIFY (the chatbot)
  ------------------------------------------
  5. When the user asks to see a specific section, call get_section_content.
     Show the full content.
  6. When the user requests a change (any of these patterns):
       "Make X shorter"
       "Rewrite X to focus on Y"
       "Add a table/chart/list to X"
       "Change the tone of X"
       "X needs more detail about Y"
     → Identify which section they mean from context or ask to confirm.
     → Call modify_section(section_id, user's exact instruction).
     → Show the new content.
     → Confirm: "Updated! The other sections were not changed."
  7. The user can modify the same section multiple times — each modification
     creates a new version. Previous versions are preserved.

  PHASE 3 — EXPORT
  ----------------
  8. When the user is satisfied, call export_document(job_id, format).
  9. Report the file path or download URL.

TIPS:
  - job_id and project_id are stored in session state — check there before asking.
  - When listing sections, always show section title + word count.
  - After any modification, always show the updated content so the user can
    review it immediately.
  - Never make up section content — always use the tool output.
  - If generation fails for a section, tell the user which one and offer to retry.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Agent definition
# ─────────────────────────────────────────────────────────────────────────────

document_generator_agent = LlmAgent(
    name        = "DocumentGeneratorAgent",
    model       = get_agent_model("DocumentGeneratorAgent"),
    description = (
        "Generates business documents (BRD, RFP, SOW, Proposals) section-by-section "
        "from the saved project context. Provides a chatbot interface for modifying "
        "individual sections — only the requested section is regenerated, never the "
        "full document. Also handles document export (Word, PDF, Markdown). "
        "Call this agent when the user wants to generate, review, modify, or export a document."
    ),
    instruction = _INSTRUCTION,
    tools       = [
        start_generation,
        get_job_status,
        list_sections,
        get_section_content,
        modify_section,
        export_document,
    ],
)
