"""
AI Derivation of Project Extended Fields
==========================================
Called by  POST /api/projects/{project_id}/derive-fields

Flow:
  1. Load all 15 ingested fields from the Project DB row
  2. Load document content (to_llm_context) for any attached document_ids
  3. Call LLM with a structured prompt asking it to derive the 12 DerivedData fields
  4. Parse the JSON response
  5. Persist to DerivedData table and set generated_at = now()

The 12 derived fields:
  current_challenges          — expanded analysis of current pain points (beyond problem_statement)
  to_be_process               — detailed future-state process description
  success_criteria            — specific, measurable success metrics (KPIs / OKRs)
  business_requirements       — high-level business needs mapped from objectives
  functional_requirements     — system-level functional specs the solution must meet
  non_functional_requirements — NFRs: performance, security, availability, scalability
  industry_benchmarks         — comparable industry standards / best practices
  workflow                    — step-by-step process workflow for the proposed solution
  analytics_requirements      — reporting, dashboards, and data analytics needs
  systems_involved            — all systems, APIs, integrations, and data stores
  data_sources                — input data sources, feeds, and data pipelines
  constraints_dependencies    — expanded constraints + external dependencies + assumptions
"""

from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Bootstrap sys.path — same pattern as extractor.py
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
    "You are a senior business analyst and enterprise architect. "
    "Your job is to analyse project intake information and derive detailed technical and "
    "business analysis fields that will feed into a formal project document (BRD, RFP, SOW, etc.). "
    "Your output must be grounded in the information provided — do not invent facts, "
    "but do expand, infer, and structure based on what is given. "
    "Return ONLY valid JSON — no markdown fences, no explanation, no prose."
)

_USER_TMPL = """Analyse the project intake information below and derive the 12 structured fields requested.

═══════════════════════════════════════
PROJECT INTAKE DATA
═══════════════════════════════════════
Project Name:         {project_name}
Project Code:         {project_code}
Business Unit:        {business_unit}
Business Priority:    {business_priority}
Document Type:        {document_type}
Timeline:             {start_date} → {end_date}
Estimated Cost:       ₹{estimated_cost_crores} Crores

Problem Statement:
{problem_statement}

Project Objective:
{project_objective}

As-Is Processes (Current State):
{as_is_processes}

Proposed Solution:
{proposed_solution}

Technical Landscape:
{technical_landscape}

Constraints:
{constraints}

Risks:
{risks}

Stakeholders:
{stakeholders_str}

{doc_context_block}

═══════════════════════════════════════
FIELDS TO DERIVE
═══════════════════════════════════════

Return EXACTLY this JSON (all 12 fields, each a detailed string of 150–400 words):

{{
  "current_challenges": "Detailed analysis of current pain points, inefficiencies, and business impacts. Go beyond the problem statement — quantify impacts where possible (e.g. cost, time, error rates). Describe the root causes and how the current state creates risk or lost value.",

  "to_be_process": "Describe the future-state process after the solution is implemented. Walk through the new workflow step by step. Explain how each pain point from the current state is resolved. Include who does what, which system handles which step, and what the user/operator experience looks like.",

  "success_criteria": "List 6–10 specific, measurable success criteria (KPIs / OKRs / acceptance criteria). Each criterion should be: Specific, Measurable, time-bound where possible. Format as a structured list with metric name, target value, and measurement method.",

  "business_requirements": "Enumerate 8–12 high-level business requirements. These are WHAT the business needs (not HOW). Use 'The system shall...' or 'The solution must...' format. Cover process automation, reporting, compliance, integration, user experience, and business continuity requirements.",

  "functional_requirements": "List 10–15 functional requirements describing WHAT the system must do technically. Organize by module or component where applicable. Each requirement should be testable. Cover core features, integrations, data flows, user roles, and key transactions.",

  "non_functional_requirements": "Enumerate NFRs across: Performance (response time, throughput, concurrent users), Availability (uptime SLA, RTO/RPO), Security (authentication, authorization, encryption, audit trail), Scalability, Maintainability, Compliance (regulatory), and Data Retention.",

  "industry_benchmarks": "Reference 4–6 relevant industry benchmarks, standards, or best practices applicable to this project. Consider ISO standards, TOGAF, ITIL, IEEE standards, sector-specific regulations, and relevant compliance frameworks. Explain how each benchmark applies to this project.",

  "workflow": "Describe the end-to-end process workflow for the proposed solution in 6–10 numbered steps. For each step include: actor (human or system), action taken, system involved, inputs required, outputs produced, and any decision points or exception flows.",

  "analytics_requirements": "Detail the analytics, reporting, and dashboard requirements: What KPI dashboards are needed? Who are the consumers (management, operations, compliance)? What data refresh frequency? What drill-down capabilities? What alerts/thresholds? What export formats? What historical analysis depth?",

  "systems_involved": "List ALL systems, applications, APIs, databases, and third-party services that will be integrated or affected. For each: system name, type (ERP/SCADA/API/DB/etc.), current version if known, integration method (REST/SOAP/DB link/file/etc.), data exchanged, and integration owner/team.",

  "data_sources": "Enumerate all data sources that feed into the solution: primary transactional systems, sensor feeds, external data providers, legacy extracts, manual inputs, third-party APIs. For each: source name, data type, volume (records/day or GB), refresh frequency, data quality concerns, and owner.",

  "constraints_dependencies": "Expand into structured constraints and dependencies: Technical Constraints (infrastructure limitations, technology mandates), Regulatory Constraints (data residency, GDPR, compliance requirements), Organizational Constraints (change management, training, parallel run period), External Dependencies (vendor delivery, third-party API availability, approvals), and Assumptions (what must remain true for the project to succeed)."
}}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def derive_project_fields(project_data: dict, document_ids: list[str]) -> dict:
    """
    Derive the 12 DerivedData fields from project ingested data + optional document context.

    Args:
        project_data:  Dict from Project.to_full_dict() — all 15 ingested fields
        document_ids:  List of document UUIDs to include as additional context

    Returns:
        Dict with the 12 derived field keys.
        All keys present even on failure (empty strings on failure).

    Raises:
        RuntimeError on LLM call failure (caller should catch and return 500)
    """
    # Build stakeholders string
    stakeholders = project_data.get("stakeholders") or []
    if isinstance(stakeholders, list):
        stakeholders_str = "\n".join(
            f"  • {s.get('name', 'Unknown')} — {s.get('designation', 'N/A')}"
            for s in stakeholders
        ) or "  Not specified"
    else:
        stakeholders_str = str(stakeholders) or "  Not specified"

    # Load document context if any documents are attached
    doc_context_block = _load_document_context(document_ids)

    user_prompt = _USER_TMPL.format(
        project_name         = project_data.get("project_name")          or "Not provided",
        project_code         = project_data.get("project_code")          or "N/A",
        business_unit        = project_data.get("business_unit")         or "Not specified",
        business_priority    = project_data.get("business_priority")     or "Not specified",
        document_type        = project_data.get("document_type")         or "BRD",
        start_date           = project_data.get("start_date")            or "Not specified",
        end_date             = project_data.get("end_date")              or "Not specified",
        estimated_cost_crores= project_data.get("estimated_cost_crores") or "Not specified",
        problem_statement    = project_data.get("problem_statement")     or "Not provided",
        project_objective    = project_data.get("project_objective")     or "Not provided",
        as_is_processes      = project_data.get("as_is_processes")       or "Not provided",
        proposed_solution    = project_data.get("proposed_solution")     or "Not provided",
        technical_landscape  = project_data.get("technical_landscape")   or "Not provided",
        constraints          = project_data.get("constraints")           or "None stated",
        risks                = project_data.get("risks")                 or "None stated",
        stakeholders_str     = stakeholders_str,
        doc_context_block    = doc_context_block,
    )

    logger.info(
        "[DeriveFields] Starting derivation for project '%s', prompt_len=%d chars",
        project_data.get("project_name", "unknown"), len(user_prompt),
    )

    return _call_llm(user_prompt)


# ─────────────────────────────────────────────────────────────────────────────
# Document context loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_document_context(document_ids: list[str]) -> str:
    """Load and concatenate LLM context from parsed documents (capped at 20k chars)."""
    if not document_ids:
        return ""

    try:
        from storage.azure_storage import get_storage_service
        from models.meta_schema    import ParsedDocument

        store    = get_storage_service()
        contexts: list[str] = []

        for doc_id in document_ids:
            try:
                meta = store.get_meta_json(doc_id)
                doc  = ParsedDocument(**meta)
                ctx  = doc.to_llm_context(max_chars=8000)
                contexts.append(f"=== Source Document: {doc.source_filename} ===\n{ctx}")
                logger.info("[DeriveFields] Loaded doc %s (%d chars)", doc.source_filename, len(ctx))
            except Exception as exc:
                logger.warning("[DeriveFields] Could not load document %s: %s", doc_id, exc)

        if not contexts:
            return ""

        combined = "\n\n".join(contexts)[:20_000]
        return (
            "═══════════════════════════════════════\n"
            "SOURCE DOCUMENT CONTENT\n"
            "Use this as additional factual reference:\n"
            "═══════════════════════════════════════\n"
            + combined
        )

    except Exception as exc:
        logger.warning("[DeriveFields] Document context loading failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# LLM call
# ─────────────────────────────────────────────────────────────────────────────

_DERIVED_FIELDS = [
    "current_challenges",
    "to_be_process",
    "success_criteria",
    "business_requirements",
    "functional_requirements",
    "non_functional_requirements",
    "industry_benchmarks",
    "workflow",
    "analytics_requirements",
    "systems_involved",
    "data_sources",
    "constraints_dependencies",
]

def _empty_derived() -> dict:
    return {f: "" for f in _DERIVED_FIELDS}


def _call_llm(user_prompt: str) -> dict:
    """
    Call the LLM via llm_provider (Gemini primary → Azure GPT-5 fallback).
    Derives all 12 DerivedData fields and returns them as a validated dict.

    Raises:
        RuntimeError: if all providers fail.
    """
    # Merge system + user into one message — required for GPT-5 reasoning model.
    combined = f"{_SYSTEM}\n\n---\n\n{user_prompt}"

    logger.info("[DeriveFields] Sending derivation prompt to LLM (prompt_len=%d chars)", len(combined))

    # Import here (not at module level) so the module loads even if llm_provider
    # dependencies aren't installed yet.
    sys_path_base = Path(__file__).parent.parent.resolve()
    if str(sys_path_base) not in sys.path:
        sys.path.insert(0, str(sys_path_base))

    from llm_provider import call_with_fallback

    try:
        raw, provider = call_with_fallback(
            messages = [{"role": "user", "content": combined}],
            # Gemini: max_tokens maps to max_output_tokens (up to 8192 on Flash).
            # GPT-5 reasoning: max_completion_tokens includes hidden reasoning tokens;
            #   12 fields × ~300 words × 6 tokens/word ≈ 21 600 tokens minimum.
            max_tokens            = 16_000,
            max_completion_tokens = 32_000,
            timeout               = 180,
            log_prefix            = "[DeriveFields]",
        )
    except RuntimeError:
        raise   # propagate to Flask route → HTTP 502

    logger.info("[DeriveFields] LLM response received via provider=%s len=%d", provider, len(raw))

    # Strip accidental markdown fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw   = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("[DeriveFields] LLM returned invalid JSON (provider=%s): %s", provider, exc)
        raise RuntimeError(f"LLM ({provider}) returned invalid JSON: {exc}") from exc

    # Validate — ensure all 12 keys are present (fill missing with empty string)
    result = _empty_derived()
    for field in _DERIVED_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            result[field] = val.strip()

    filled = sum(1 for v in result.values() if v)
    logger.info(
        "[DeriveFields] Derivation complete via %s — %d/12 fields populated",
        provider, filled,
    )
    return result
