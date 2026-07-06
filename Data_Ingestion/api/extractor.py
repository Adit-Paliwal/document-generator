"""
LLM-powered extraction of project form fields from parsed documents.
=========================================================================
Called by  POST /api/extract-project-data.

Flow:
  1. Load each document's parsed content via ParsedDocument.to_llm_context()
  2. Concatenate (capped at 60 K chars so it fits in one LLM call)
  3. Ask the LLM to return a JSON object matching ProjectFormData fields
  4. Compute which required fields are still missing
  5. Return  { extracted: {...}, missing_required: [...], missing_optional: [...] }

The function raises RuntimeError on LLM failure so the caller can return HTTP 502
with a clear error message (rather than silently returning an empty form).
"""

from __future__ import annotations
import json
import logging
import os
import sys
from pathlib import Path

# Bootstrap sys.path — same pattern as agent.py
_BASE = Path(__file__).parent.parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_BASE / ".env", override=False)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are an expert business analyst and requirements engineer. "
    "Your job is to read document content (which may be a BRD, requirements doc, "
    "proposal, presentation, spreadsheet, or any other business document) and extract "
    "structured project information to fill a standard project intake form. "
    "Be creative in mapping — e.g. a document title is the project name, a purpose/scope "
    "section maps to problem_statement and project_objective, a stakeholders table maps "
    "to stakeholders, etc. "
    "The document content may include AI-described images embedded as tags of the form "
    "[IMAGE [<type>]: <description>] where <type> can be workflow_flowchart, "
    "architecture_diagram, chart_graph, table_screenshot, or ui_screenshot. "
    "Treat these image descriptions as first-class content — extract information from them "
    "exactly as you would from text. "
    "Return ONLY valid JSON — no markdown fences, no explanation, no prose."
)

_USER_TMPL = """Analyse the document content below and fill in as many of the following
project intake form fields as possible.

MAPPING GUIDANCE:
- business_unit: the company, department, or division this project belongs to.
  Use the organisation or team name mentioned in the document. Leave null if truly unknown.
- project_name: the main title or name of the project / document (required).
- project_code: any project ID, reference number, version, or code (e.g. PROJ-2026-001).
- problem_statement: WHY this project exists — the business problem, pain point, or
  challenge. Extract from Purpose, Background, Problem, Objective, or Scope sections.
- project_objective: WHAT success looks like — goals, objectives, expected outcomes.
- stakeholders: list every person/role/team mentioned as a stakeholder, owner, or contact.
  For each person use their full name and role/designation.
- start_date / end_date: any project timeline dates. Format as YYYY-MM-DD.
- as_is_processes: current (existing) processes, workflows, tools, or challenges described.
- proposed_solution: the recommended or proposed approach, solution, or design.
- constraints: any stated limitations, dependencies, assumptions, or constraints.
- risks: any risk items, issues, or mitigation strategies mentioned.
- technical_landscape: systems, integrations, APIs, databases, or tech stack mentioned.
- estimated_cost_crores: any budget figure — convert to Crores INR if possible (e.g. "12.5").
- business_priority: one of "Critical", "Highly Critical", "Non-Critical" — infer from urgency
  language in the document. Default to "Non-Critical" if no clear signal.

IMAGE TAGS — the document may contain embedded image descriptions in this format:
  [IMAGE [workflow_flowchart]: <description> Key components: ...]
  [IMAGE [architecture_diagram]: <description> Key components: ...]
  [IMAGE [chart_graph]: <description> ...]
  [IMAGE [table_screenshot]: <description> ...]
  [IMAGE [ui_screenshot]: <description> ...]
Extract information from these image descriptions just like text:
  • workflow_flowchart  → map to as_is_processes (current workflow) or proposed_solution (future workflow)
  • architecture_diagram → map to technical_landscape (systems, integrations, tech stack)
  • chart_graph         → may reveal KPIs, metrics, or business context → problem_statement / project_objective
  • table_screenshot    → may reveal stakeholders, timelines, costs, or requirements
  • ui_screenshot       → may reveal proposed solution design or system interfaces

Return EXACTLY this JSON structure (use null for fields you cannot find or reliably infer):

{
  "business_unit":          null,
  "project_name":           null,
  "project_code":           null,
  "problem_statement":      null,
  "project_objective":      null,
  "stakeholders": [
    {"name": "<person name or role title>", "designation": "<role/designation>"}
  ],
  "start_date":             null,
  "end_date":               null,
  "as_is_processes":        null,
  "proposed_solution":      null,
  "constraints":            null,
  "risks":                  null,
  "technical_landscape":    null,
  "estimated_cost_crores":  null,
  "business_priority":      null
}

DOCUMENT CONTENT:
<<DOCUMENT_CONTENT>>
"""

# All 12 fields required by the Create Project form
_REQUIRED_FIELDS = [
    "business_unit",
    "project_name",
    "project_code",
    "problem_statement",
    "project_objective",
    "stakeholders",
    "start_date",
    "end_date",
    "as_is_processes",
    "proposed_solution",
    "technical_landscape",
    "business_priority",
]

# Human-readable labels shown to the user for missing fields
_FIELD_LABELS = {
    "business_unit":         "Business Unit / Department",
    "project_name":          "Project Name",
    "project_code":          "Project Code / ID",
    "problem_statement":     "Problem Statement — what business problem does this solve?",
    "project_objective":     "Project Objective — what are the goals and expected outcomes?",
    "stakeholders":          "Stakeholders — names and designations",
    "start_date":            "Project Start Date",
    "end_date":              "Project End Date",
    "as_is_processes":       "As-Is Processes & Challenges",
    "proposed_solution":     "Proposed Solution Overview",
    "constraints":           "Constraints & Dependencies",
    "risks":                 "Risk & Mitigation",
    "technical_landscape":   "Technical Landscape & Integrations",
    "estimated_cost_crores": "Project Estimated Cost (₹ Crores)",
    "business_priority":     "Business Priority & Criticality (Critical / Highly Critical / Non-Critical)",
}


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def extract_project_data(document_ids: list[str]) -> dict:
    """
    Load the given parsed documents and call the LLM to extract form fields.

    Args:
        document_ids: list of document UUIDs (returned by POST /api/upload)

    Returns:
        {
            "extracted":         { ...form fields... },
            "missing_required":  [ {"field": "...", "label": "...", "question": "..."} ],
            "missing_optional":  [ {"field": "...", "label": "..."} ],
            "filled_count":      <int>,
            "total_fields":      <int>,
        }

    Raises:
        RuntimeError: if the LLM call fails. Caller should return HTTP 502.
        FileNotFoundError: if a document_id is not found. Caller should return HTTP 404.
    """
    from storage.gcs_storage import get_storage_service
    from models.meta_schema    import ParsedDocument

    store    = get_storage_service()
    contexts: list[str] = []

    for doc_id in document_ids:
        try:
            meta = store.get_meta_json(doc_id)
            doc  = ParsedDocument(**meta)
            ctx  = doc.to_llm_context(max_chars=15000)
            contexts.append(f"=== File: {doc.source_filename} ===\n{ctx}")
            logger.info("[Extractor] Loaded %s (%d chars)", doc.source_filename, len(ctx))
        except FileNotFoundError:
            raise
        except Exception as exc:
            logger.warning("[Extractor] Could not load document %s: %s", doc_id, exc)

    if not contexts:
        logger.warning("[Extractor] No document content loaded — returning empty")
        return _build_response({})

    combined = "\n\n".join(contexts)[:60_000]   # stay well within context window
    extracted = _call_llm(combined)             # raises RuntimeError on failure
    return _build_response(extracted)


# ─────────────────────────────────────────────────────────────────────────────
# Missing-fields analysis
# ─────────────────────────────────────────────────────────────────────────────

# Questions the agent "asks" when a required field is missing
_MISSING_QUESTIONS = {
    "business_unit":      "Which business unit or department does this project belong to?",
    "project_name":       "What is the full name of this project?",
    "project_code":       "What is the project code or reference ID? (e.g. PROJ-2026-001)",
    "problem_statement":  "What business problem or challenge is this project solving? Describe the current pain points.",
    "project_objective":  "What are the main objectives and expected outcomes of this project?",
    "stakeholders":       "Who are the key stakeholders? Please provide names and their roles/designations.",
    "start_date":         "What is the planned project start date? (YYYY-MM-DD)",
    "end_date":           "What is the planned project end date? (YYYY-MM-DD)",
    "as_is_processes":    "Describe the current (as-is) processes, workflows, tools, and challenges.",
    "proposed_solution":  "What is the proposed solution or approach for this project?",
    "technical_landscape":"What systems, technologies, integrations, or data sources are involved?",
    "business_priority":  "How critical is this project? (Critical / Highly Critical / Non-Critical)",
}

def _is_empty(value) -> bool:
    """Returns True if a field value should be treated as 'missing'."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False

def _build_response(extracted: dict) -> dict:
    """
    Given an extracted dict, compute missing_required and missing_optional lists.
    Returns the full response envelope.
    """
    all_fields = list(_FIELD_LABELS.keys())
    missing_required = []
    missing_optional = []

    for field in all_fields:
        val = extracted.get(field)
        if _is_empty(val):
            entry = {"field": field, "label": _FIELD_LABELS[field]}
            if field in _REQUIRED_FIELDS:
                if field in _MISSING_QUESTIONS:
                    entry["question"] = _MISSING_QUESTIONS[field]
                missing_required.append(entry)
            else:
                missing_optional.append(entry)

    filled_count = sum(
        1 for f in all_fields if not _is_empty(extracted.get(f))
    )

    return {
        "extracted":        extracted,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "filled_count":     filled_count,
        "total_fields":     len(all_fields),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM call — delegates to llm_provider (Gemini via Vertex AI)
# ─────────────────────────────────────────────────────────────────────────────

def _call_llm(content: str) -> dict:
    """
    Build the extraction prompt and call the LLM via llm_provider.
    Provider: Gemini Vertex AI (GCP).

    Raises:
        RuntimeError: if Gemini fails — caller converts to HTTP 502.
    """
    # Use plain .replace() so document content with { } braces
    # doesn't break Python string formatting.
    prompt = _USER_TMPL.replace("<<DOCUMENT_CONTENT>>", content)

    combined_prompt = f"{_SYSTEM}\n\n---\n\n{prompt}"

    logger.info("[Extractor] Sending prompt to LLM (content_len=%d chars)", len(content))

    from llm_provider import call_with_fallback
    try:
        raw, provider = call_with_fallback(
            messages   = [{"role": "user", "content": combined_prompt}],
            max_tokens = 8_000,
            timeout    = 120,
            log_prefix = "[Extractor]",
        )
    except RuntimeError:
        raise   # propagate clean error to the Flask route → HTTP 502

    logger.info("[Extractor] LLM response received via provider=%s len=%d", provider, len(raw))

    # Strip accidental markdown fences (some models wrap JSON in ```json ... ```)
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw   = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("[Extractor] LLM returned invalid JSON (provider=%s): %s", provider, exc)
        raise RuntimeError(f"LLM ({provider}) returned invalid JSON: {exc}") from exc

    # Unwrap single-key wrapper e.g. {"result": {...}} or {"extracted": {...}}
    if isinstance(data, dict) and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, dict) and "project_name" in inner:
            data = inner

    logger.info(
        "[Extractor] Extraction OK via %s — keys: %s",
        provider, sorted(data.keys()),
    )
    return data
