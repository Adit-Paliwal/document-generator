"""
ContextCollector Tools
=======================
ADK tools for Agent 2 — ContextCollectorAgent.

Responsibility:
  Load the project context that was saved via the frontend form
  (both user-ingested fields AND AI-derived fields) and package it
  into a rich context object that the DocumentGeneratorAgent can use.

Flow context:
  Frontend:  User uploads doc → clicks Extract → form pre-fills → user edits → saves project
  This agent: Reads that saved project from DB and presents it to the generator.

Tools:
  load_project_context   — fetches ingested + derived data for a project_id
  validate_context        — checks which required fields are filled / missing
  get_generation_context  — returns a formatted context string for generation prompts
"""

from __future__ import annotations
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# Ensure Data_Ingestion/ is on sys.path
_BASE = Path(__file__).parent.parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from google.adk.tools import ToolContext  # noqa: E402

logger = logging.getLogger(__name__)

# Required fields that must be present for generation to proceed
_REQUIRED_FIELDS = [
    "project_name",
    "problem_statement",
    "project_objective",
    "proposed_solution",
    "technical_landscape",
    "business_unit",
    "business_priority",
    "start_date",
    "end_date",
]


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — load_project_context
# ─────────────────────────────────────────────────────────────────────────────

async def load_project_context(
    project_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Load the full project context from the database.

    Fetches BOTH:
      - Ingested data: what the user filled in via the Create Project form
        (project_name, problem_statement, stakeholders, timeline, etc.)
      - Derived data: AI-extracted fields from the uploaded documents
        (functional_requirements, success_criteria, workflow, etc.)

    Stores the combined context in session state so other agents
    (DocumentGeneratorAgent) can access it without re-fetching.

    Args:
        project_id: UUID of the saved project (from POST /api/projects response).

    Returns:
        dict with 'ingested', 'derived', 'document_ids', and 'ready_for_generation'.
    """
    try:
        from generation.db import Project, DerivedData, get_session

        with get_session() as session:
            proj = session.get(Project, project_id)
            if not proj:
                return {"error": f"Project '{project_id}' not found. Has the form been saved?"}

            ingested = proj.to_ingested_dict()
            derived_row = session.get(DerivedData, project_id)
            derived = derived_row.to_dict() if derived_row else {}

        # Merge into a single context object
        context = {
            "project_id":    project_id,
            "project_name":  ingested.get("project_name", ""),
            "document_type": ingested.get("document_type", "BRD"),
            "output_format": ingested.get("output_format", "Word (.docx)"),
            "document_ids":  ingested.get("document_ids", []),
            "template_id":   ingested.get("template_id", ""),
            "ingested":      ingested,
            "derived":       derived,
        }

        # Persist in session state for DocumentGeneratorAgent
        tool_context.state["project_id"]          = project_id
        tool_context.state["project_context"]     = context
        tool_context.state["document_ids"]        = ingested.get("document_ids", [])
        tool_context.state["document_type"]       = ingested.get("document_type", "BRD")

        # Count how many derived fields have been populated
        derived_filled = sum(
            1 for k, v in derived.items()
            if k not in ("project_id", "generated_at", "updated_at") and v
        )

        return {
            **context,
            "derived_fields_populated": derived_filled,
            "message": (
                f"Loaded project '{ingested.get('project_name')}' "
                f"(type: {ingested.get('document_type')}, "
                f"docs: {len(ingested.get('document_ids', []))}, "
                f"derived fields: {derived_filled} populated). "
                "Context is ready for generation."
            ),
        }

    except Exception as e:
        logger.exception("load_project_context failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — validate_context
# ─────────────────────────────────────────────────────────────────────────────

async def validate_context(
    project_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Check which required fields are filled and which are missing.
    Also confirms that at least one source document is attached.

    Args:
        project_id: UUID of the saved project.

    Returns:
        dict with 'missing', 'filled', 'has_documents', 'ready' (bool).
    """
    try:
        from generation.db import Project, get_session

        with get_session() as session:
            proj = session.get(Project, project_id)
            if not proj:
                return {"error": f"Project '{project_id}' not found."}
            ingested = proj.to_ingested_dict()

        missing = []
        filled  = []
        for field in _REQUIRED_FIELDS:
            val = ingested.get(field, "")
            if val and str(val).strip():
                filled.append(field)
            else:
                missing.append(field)

        # Stakeholders check — must have at least one with a name
        stakeholders = ingested.get("stakeholders", [])
        has_stakeholders = any(s.get("name", "").strip() for s in stakeholders)
        if not has_stakeholders:
            missing.append("stakeholders")
        else:
            filled.append("stakeholders")

        doc_ids      = ingested.get("document_ids", [])
        has_documents = len(doc_ids) > 0
        if not has_documents:
            missing.append("source_documents")

        ready = len(missing) == 0

        return {
            "project_id":     project_id,
            "missing":        missing,
            "filled":         filled,
            "has_documents":  has_documents,
            "document_count": len(doc_ids),
            "ready":          ready,
            "message": (
                "All required fields are present. Ready for generation!"
                if ready else
                f"Missing {len(missing)} required item(s): {', '.join(missing)}. "
                "Please complete the form before generating."
            ),
        }

    except Exception as e:
        logger.exception("validate_context failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — get_generation_context
# ─────────────────────────────────────────────────────────────────────────────

async def get_generation_context(
    project_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Return a concise, formatted summary of the project context.
    Shows the user what information will be used for generation,
    combining both ingested (user-entered) and derived (AI-extracted) fields.

    Args:
        project_id: UUID of the saved project.

    Returns:
        dict with a 'summary' string and structured fields.
    """
    try:
        # Try session state first (already loaded by load_project_context)
        ctx = tool_context.state.get("project_context")

        if not ctx or ctx.get("project_id") != project_id:
            # Re-load from DB if not in state
            result = await load_project_context(project_id, tool_context)
            if "error" in result:
                return result
            ctx = tool_context.state.get("project_context", {})

        ingested = ctx.get("ingested", {})
        derived  = ctx.get("derived",  {})

        # Build a readable summary
        sth_list = ingested.get("stakeholders", [])
        sth_str  = ", ".join(
            f"{s.get('name')} ({s.get('designation', '')})"
            for s in sth_list if s.get("name")
        ) or "Not specified"

        summary_lines = [
            f"Project:       {ingested.get('project_name', 'N/A')}",
            f"Code:          {ingested.get('project_code', 'N/A')}",
            f"Business Unit: {ingested.get('business_unit', 'N/A')}",
            f"Priority:      {ingested.get('business_priority', 'N/A')}",
            f"Timeline:      {ingested.get('start_date', 'N/A')} → {ingested.get('end_date', 'N/A')}",
            f"Document Type: {ingested.get('document_type', 'BRD')}",
            f"Stakeholders:  {sth_str}",
            "",
            "--- User-entered context ---",
            f"Problem:       {ingested.get('problem_statement', 'N/A')[:200]}",
            f"Objective:     {ingested.get('project_objective', 'N/A')[:200]}",
            f"Solution:      {ingested.get('proposed_solution', 'N/A')[:200]}",
            f"As-Is:         {ingested.get('as_is_processes', 'N/A')[:200]}",
            f"Tech:          {ingested.get('technical_landscape', 'N/A')[:200]}",
        ]

        # Add derived fields if populated
        derived_highlights = [
            ("Functional Req.", derived.get("functional_requirements", "")),
            ("Non-Func. Req.",  derived.get("non_functional_requirements", "")),
            ("Success Criteria", derived.get("success_criteria", "")),
            ("Workflow",        derived.get("workflow", "")),
            ("Systems",         derived.get("systems_involved", "")),
        ]
        populated_derived = [(k, v) for k, v in derived_highlights if v and str(v).strip()]
        if populated_derived:
            summary_lines.append("")
            summary_lines.append("--- AI-derived context ---")
            for label, val in populated_derived:
                summary_lines.append(f"{label}: {str(val)[:200]}")

        return {
            "project_id":   project_id,
            "summary":      "\n".join(summary_lines),
            "document_ids": ingested.get("document_ids", []),
            "document_type": ingested.get("document_type", "BRD"),
            "has_derived":  bool(populated_derived),
            "message":      f"Context loaded for '{ingested.get('project_name')}'. Ready to generate.",
        }

    except Exception as e:
        logger.exception("get_generation_context failed")
        return {"error": f"{type(e).__name__}: {e}"}
