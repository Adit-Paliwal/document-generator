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
    # Long-form canonical names
    "Business Requirements Document (BRD)": "brd",
    "Request for Proposal (RFP)":           "rfp",
    "Statement of Work (SOW)":              "sow",
    "Project Proposal":                     "proposal",
    "Technical Specification":              "tech_spec",
    "Scope Document":                       "scope",
    # Short-form aliases (from frontend dropdowns)
    "BRD":                                  "brd",
    "RFP":                                  "rfp",
    "SOW":                                  "sow",
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
                    continue   # already seeded

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
    Returns list of template.to_dict() dicts.
    """
    ensure_seeded()
    with get_session() as session:
        q = session.query(Template)
        if document_type:
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
