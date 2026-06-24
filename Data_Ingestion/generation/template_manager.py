"""
Template Manager
=================
Loads prompt templates from JSON files in the templates/ directory.
Seeds system templates into the database on first run.
Provides the section list for a given document type or template ID.
"""

from __future__ import annotations
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from generation.db import Template, get_session

logger = logging.getLogger(__name__)

# Path to the JSON template files — relative to this file's parent (Data_Ingestion/)
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Map document_type strings → template JSON file IDs.
# Both long-form (canonical) and short-form (from user_input_schema.py dropdown)
# are registered explicitly — no fragile fuzzy matching needed.
_DOC_TYPE_MAP: dict[str, str] = {
    # Long-form canonical names (match what template JSON files store in document_type)
    "Business Requirements Document (BRD)":         "brd",
    "Request for Proposal (RFP)":                   "rfp",
    "Statement of Work (SOW)":                      "sow",
    "Project Proposal":                             "proposal",
    "Technical Specification":                      "tech_spec",
    "Scope Document":                               "scope",
    "Non-Detailed Project Report (NDPR)":           "ndpr",
    "Note for Approval (NFA)":                      "nfa",
    "Notice Inviting Tender (NIT)":                 "nit",
    "Bill of Quantities (BOQ)":                     "boq",
    "Architecture Review Board (ARB) Submission":   "arb",
    # Short-form aliases (from frontend document_type dropdowns)
    "BRD":      "brd",
    "RFP":      "rfp",
    "SOW":      "sow",
    "NDPR":     "ndpr",
    "NFA":      "nfa",
    "NIT":      "nit",
    "BOQ":      "boq",
    "ARB":      "arb",
}

_seeded = False   # guard against repeated seeding in the same process


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def ensure_seeded() -> None:
    """
    Seed all system templates into the DB if not already present.
    Safe to call multiple times — skips templates that already exist.
    """
    global _seeded
    if _seeded:
        return

    with get_session() as session:
        for json_path in sorted(_TEMPLATES_DIR.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                template_id = data.get("id")
                if not template_id:
                    continue

                existing = session.get(Template, template_id)
                if existing:
                    if existing.is_system:
                        # Always refresh system templates from JSON so that edits
                        # to brd.json (column names, instructions, etc.) are picked
                        # up immediately on the next process start — no manual
                        # reseed needed.
                        existing.name                = data["name"]
                        existing.description         = data.get("description")
                        existing.sections_config     = json.dumps(data.get("sections", []))
                        existing.system_instructions = data.get("system_instructions")
                        logger.info("Refreshed system template: %s", template_id)
                    continue   # user templates are never overwritten

                tmpl = Template(
                    template_id         = template_id,
                    name                = data["name"],
                    document_type       = data["document_type"],
                    description         = data.get("description"),
                    sections_config     = json.dumps(data.get("sections", [])),
                    system_instructions = data.get("system_instructions"),
                    is_system           = True,
                )
                session.add(tmpl)
                logger.info("Seeded template: %s (%s)", template_id, data["name"])

            except Exception as e:
                logger.warning("Failed to seed template %s: %s", json_path.name, e)

        session.commit()

    _seeded = True


def get_template_for_doc_type(document_type: str) -> Optional[Template]:
    """
    Return the system template for a given document_type string.
    Accepts both long-form ("Business Requirements Document (BRD)") and
    short-form ("BRD") names — both are registered in _DOC_TYPE_MAP.
    Returns None if no matching template exists.
    """
    ensure_seeded()
    template_id = _DOC_TYPE_MAP.get(document_type)
    if not template_id:
        return None

    with get_session() as session:
        return session.get(Template, template_id)


def get_template_by_id(template_id: str) -> Optional[Template]:
    """Return a template by its ID (system or user-created)."""
    ensure_seeded()
    with get_session() as session:
        return session.get(Template, template_id)


def list_templates(document_type: Optional[str] = None) -> list[dict]:
    """
    Return all templates, optionally filtered by document_type.
    Accepts both short aliases ("BRD") and canonical names
    ("Business Requirements Document (BRD)") — both return the same results.
    """
    ensure_seeded()
    with get_session() as session:
        q = session.query(Template)
        if document_type:
            resolved_id = _DOC_TYPE_MAP.get(document_type)
            if resolved_id:
                from sqlalchemy import or_
                q = q.filter(or_(
                    Template.document_type == document_type,
                    Template.template_id == resolved_id,
                ))
            else:
                q = q.filter(Template.document_type == document_type)
        return [t.to_dict() for t in q.order_by(Template.is_system.desc(), Template.name).all()]


def get_sections_for_job(
    document_type: str,
    template_id: Optional[str],
    sections_override: Optional[list[str]],
) -> list[dict]:
    """
    Return the ordered list of section configs for a generation job.

    Priority:
      1. Explicit template_id (user-selected template)
      2. Default template for document_type
      3. Minimal fallback (single "Content" section)

    If sections_override is provided (list of section_keys), only those
    sections are included (in template order).
    """
    ensure_seeded()

    template = None
    if template_id:
        template = get_template_by_id(template_id)
    if template is None:
        template = get_template_for_doc_type(document_type)

    if template:
        sections = template.sections_list()
    else:
        # Fallback for unrecognised document types
        sections = [
            {
                "key":          "content",
                "title":        document_type,
                "order":        1,
                "instructions": f"Generate a professional {document_type} based on the provided document context and project information. Structure the content logically with appropriate headings.",
                "target_words": 500,
            }
        ]

    if sections_override:
        override_set = {s.lower().replace(" ", "_") for s in sections_override}
        sections = [
            s for s in sections
            if s["key"] in override_set or s["title"].lower() in {o.lower() for o in sections_override}
        ]
        # Re-number order
        for i, s in enumerate(sections):
            s["order"] = i + 1

    return sections


def reseed_template(template_id: str) -> bool:
    """
    Delete an existing system template from the DB and re-seed it from its JSON file.
    Use this after updating a template JSON (e.g. brd.json) to push the changes to DB.

    Returns True if the template was re-seeded successfully, False if the JSON wasn't found.
    """
    global _seeded

    json_path = _TEMPLATES_DIR / f"{template_id}.json"
    if not json_path.exists():
        logger.error("Template JSON not found: %s", json_path)
        return False

    data = json.loads(json_path.read_text(encoding="utf-8"))
    if data.get("id") != template_id:
        logger.error("Template ID mismatch in %s: expected %s", json_path, template_id)
        return False

    with get_session() as session:
        existing = session.get(Template, template_id)
        if existing:
            session.delete(existing)
            session.commit()
            logger.info("Deleted existing template: %s", template_id)

        tmpl = Template(
            template_id         = template_id,
            name                = data["name"],
            document_type       = data["document_type"],
            description         = data.get("description"),
            sections_config     = json.dumps(data.get("sections", [])),
            system_instructions = data.get("system_instructions"),
            is_system           = True,
        )
        session.add(tmpl)
        session.commit()
        logger.info("Re-seeded template: %s (%s sections)", template_id, len(data.get("sections", [])))

    _seeded = False   # force next ensure_seeded() to refresh
    return True


def save_user_template(
    name: str,
    document_type: str,
    sections: list[dict],
    system_instructions: Optional[str] = None,
    description: Optional[str] = None,
) -> Template:
    """Create and persist a user-defined template. Returns the saved Template."""
    with get_session() as session:
        tmpl = Template(
            template_id         = str(uuid.uuid4()),
            name                = name,
            document_type       = document_type,
            description         = description,
            sections_config     = json.dumps(sections),
            system_instructions = system_instructions,
            is_system           = False,
        )
        session.add(tmpl)
        session.commit()
        session.refresh(tmpl)
        return tmpl
