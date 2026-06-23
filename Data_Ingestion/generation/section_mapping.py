"""
Section Mapping — per-section generation guidance from the Adani mapping document.

The mapping document (MAPPING_DOCUMENT_FOR_AGENT.xlsx) was prepared by the client
team from real Adani document templates. It defines, for each document type and
section:
  - What the section should contain (description)
  - Required columns/fields (variables) — critical for table sections
  - Output format (Table / Text / List / mixed)
  - ID format conventions (FR-001, BRQ-001, UC1 etc.)
  - Depth level (Detailed / Short) from section_config
  - Which BU input fields from the project form supply data for this section

This module compiles that information into LLM prompt guidance injected by
generator.py immediately before each section is generated.

Data source: generation/mapping_data/section_specs.json
  (compiled from the XLSX by running the compile_mapping.py script — do not
   edit the JSON manually; re-run the script instead.)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_SPECS_PATH = Path(__file__).parent / "mapping_data" / "section_specs.json"

# Doc-type aliases so callers can pass "Business Requirements Document (BRD)" etc.
_DOC_ALIASES: dict[str, str] = {
    "brd": "BRD",
    "business requirements document": "BRD",
    "business requirements": "BRD",
    "rfp": "RFP",
    "request for proposal": "RFP",
    "ndpr": "NDPR",
    "note for detailed project report": "NDPR",
    "nfa": "NFA",
    "note for approval": "NFA",
    "nit": "NIT",
    "notice inviting tender": "NIT",
    "boq": "BOQ",
    "bill of quantities": "BOQ",
    "arb": "ARB",
    "architecture review board": "ARB",
}


@lru_cache(maxsize=1)
def _load_specs() -> dict:
    if not _SPECS_PATH.exists():
        return {}
    return json.loads(_SPECS_PATH.read_text(encoding="utf-8"))


def _resolve_doc_type(doc_type: str) -> str:
    """Normalize free-text doc type to the 3-5 letter key used in specs JSON."""
    key = doc_type.lower().strip()
    # Direct lookup
    if key in _DOC_ALIASES:
        return _DOC_ALIASES[key]
    # Check if any alias is a substring of the input
    for alias, resolved in _DOC_ALIASES.items():
        if alias in key:
            return resolved
    return doc_type.upper()[:5]


def _normalize(text: str) -> str:
    """Reduce a section name to alphanum-only lowercase for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def get_section_spec(doc_type: str, section_name: str) -> Optional[dict]:
    """
    Return the mapping spec for a section, or None if not found.

    Matching is done by normalising both the section name and each entry's
    'section' field to lowercase alphanumeric, then trying:
      1. Exact match on the normalised names
      2. Either name is a substring of the other

    Returns dict with keys: section, description, variables, format, remarks, depth
    """
    specs = _load_specs()
    resolved = _resolve_doc_type(doc_type)
    sections = specs.get(resolved, [])
    if not sections:
        return None

    target = _normalize(section_name)

    # Pass 1 — exact normalised match
    for entry in sections:
        if _normalize(entry["section"]) == target:
            return entry

    # Pass 2 — substring match, sorted longest-first so more specific names win.
    # e.g. "nonfunctionalrequirement" is tried before "functionalrequirement"
    # so "Non-Functional Requirements" won't accidentally match "Functional Requirement".
    by_len = sorted(sections, key=lambda e: len(_normalize(e["section"])), reverse=True)
    for entry in by_len:
        norm = _normalize(entry["section"])
        if norm in target or target in norm:
            return entry

    return None


def get_bu_input_fields(doc_type: str, section_name: str) -> list[str]:
    """
    Return the BU input field names that feed this section/attribute,
    according to the cross-document mapping sheet.

    Useful for context collection — tells the agent which project form fields
    to pull when gathering context for this section.
    """
    specs = _load_specs()
    resolved = _resolve_doc_type(doc_type)
    target = _normalize(section_name)
    fields: list[str] = []

    for entry in specs.get("cross_document_mapping", []):
        if resolved not in entry.get("documents", []):
            continue
        attr_norm = _normalize(entry.get("section_attribute", ""))
        if attr_norm == target or attr_norm in target or target in attr_norm:
            fields = entry.get("bu_input_fields", [])
            break

    return fields


def build_section_guidance(doc_type: str, section_name: str) -> str:
    """
    Build a formatted guidance block to inject into the LLM section prompt.

    Returns an empty string if no spec is found (caller proceeds without it).

    The returned block looks like:

        ## ADANI TEMPLATE SPECIFICATION
        **What to include:** Define system features/capabilities...
        **Required columns:** Requirement Number, Sr., Description, ...
        **Output format:** Table — generate a markdown pipe table with the columns above
        **IDs / numbering:** Use requirement IDs like FR-001, FR-002
        **Depth:** Detailed
        **Key input fields:** Functional requirement, Proposed solution overview

    This is appended to section_instructions in generator.py, so the LLM gets
    both the generic template instructions AND the Adani-specific structure.
    """
    spec = get_section_spec(doc_type, section_name)
    if not spec:
        return ""

    lines: list[str] = ["## ADANI TEMPLATE SPECIFICATION"]

    if spec.get("description"):
        lines.append(f"**What to include:** {spec['description']}")

    if spec.get("variables"):
        lines.append(f"**Required columns / variables:** {spec['variables']}")

    fmt = spec.get("format", "")
    if fmt:
        fmt_lower = fmt.lower()
        if "table" in fmt_lower:
            lines.append(
                f"**Output format:** Table — generate a markdown pipe table "
                f"with the columns listed above. Every row must have all columns."
            )
        elif "list" in fmt_lower:
            lines.append("**Output format:** Bulleted list (- items)")
        elif "text" in fmt_lower:
            lines.append("**Output format:** Prose paragraphs")

    if spec.get("remarks"):
        lines.append(f"**Generation notes:** {spec['remarks']}")

    depth = spec.get("depth")
    if depth in ("Detailed", "Short"):
        lines.append(f"**Depth:** {depth}")

    bu_fields = get_bu_input_fields(doc_type, section_name)
    if bu_fields:
        lines.append(f"**Key input fields to look for:** {', '.join(bu_fields)}")

    return "\n".join(lines)
