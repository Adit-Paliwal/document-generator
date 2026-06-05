"""
UI Input Schema
===============
Defines every field the user fills in via the frontend Create Project form.
Returned by GET /api/form-fields so the frontend can render the form dynamically.

Field types:
  text            — single-line text input
  textarea        — multi-line text area
  select          — single-choice dropdown
  multiselect     — multiple-choice dropdown
  date            — date picker (value: YYYY-MM-DD string)
  stakeholder_list — structured table of {name, designation} rows
  number          — numeric input (stored as string, e.g. "12.5")
"""

from __future__ import annotations
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, validator


# ─────────────────────────────────────────────────────────────────────────────
# Stakeholder sub-field config — describes the two columns of the table
# ─────────────────────────────────────────────────────────────────────────────

class StakeholderColumnConfig(BaseModel):
    key:         str         # JSON key used in each row object
    label:       str         # Column header shown in UI
    type:        Literal["text", "select"]
    options:     List[str]   = []   # dropdown choices (empty = free-text input)
    placeholder: str         = ""
    required:    bool        = True


class StakeholderConfig(BaseModel):
    columns: List[StakeholderColumnConfig]
    # Value shape sent to the API:  [{"name": "...", "designation": "..."}]


# ─────────────────────────────────────────────────────────────────────────────
# Field definition (for dynamic UI rendering)
# ─────────────────────────────────────────────────────────────────────────────

class UIField(BaseModel):
    key:                str
    label:              str
    type:               Literal["text", "textarea", "select", "multiselect",
                                "date", "stakeholder_list", "number", "checkbox"]
    required:           bool                       = False
    options:            List[str]                  = []
    placeholder:        str                        = ""
    help_text:          str                        = ""
    default:            Any                        = None
    stakeholder_config: Optional[StakeholderConfig] = None  # only for stakeholder_list


# ─────────────────────────────────────────────────────────────────────────────
# Form fields — matches the Figma Create Project design exactly
# Order matches the on-screen layout (top to bottom, left to right)
# ─────────────────────────────────────────────────────────────────────────────

DOCUMENT_FORM_FIELDS: List[UIField] = [

    # ── Row 1: 3-column ──────────────────────────────────────────────────────

    UIField(
        key         = "business_unit",
        label       = "Business Unit",
        type        = "select",
        required    = True,
        options     = [
            "AESL",
            "AESL-Digital",
            "AESL-Infrastructure",
            "AESL-Finance",
            "AESL-Operations",
        ],
        help_text   = "The Adani group company or division this project belongs to.",
    ),

    UIField(
        key         = "project_name",
        label       = "Project Name",
        type        = "text",
        required    = True,
        placeholder = "Enter project name",
    ),

    UIField(
        key         = "project_code",
        label       = "Project Code",
        type        = "text",
        required    = True,
        placeholder = "e.g. AESL-2026-001",
        help_text   = "Unique project identifier or reference number.",
    ),

    # ── Row 2: 2-column (large textareas) ────────────────────────────────────

    UIField(
        key         = "problem_statement",
        label       = "Problem Statement",
        type        = "textarea",
        required    = True,
        placeholder = "Describe the problem this project addresses...",
        help_text   = "WHY this project exists — the business problem or pain point.",
    ),

    UIField(
        key         = "project_objective",
        label       = "Project Objective",
        type        = "textarea",
        required    = True,
        placeholder = "List the main objectives and expected outcomes...",
        help_text   = "WHAT success looks like — goals and expected outcomes.",
    ),

    # ── Row 3: 2-column (stakeholders + timeline) ────────────────────────────

    UIField(
        key         = "stakeholders",
        label       = "Stakeholders",
        type        = "stakeholder_list",
        required    = True,
        help_text   = "Add at least one stakeholder. Each row has a name and designation.",
        stakeholder_config = StakeholderConfig(
            columns = [
                StakeholderColumnConfig(
                    key         = "name",
                    label       = "Name",
                    type        = "select",
                    options     = [
                        "Srinivas Gutta",
                        "Ali Imam",
                        "Bapurao Tilekar",
                        "Santosh Sawant",
                        "Rajesh Lad",
                    ],
                    placeholder = "Select or type a name",
                    required    = True,
                ),
                StakeholderColumnConfig(
                    key         = "designation",
                    label       = "Designation",
                    type        = "select",
                    options     = [
                        "Project Manager",
                        "Technical Lead",
                        "Business Analyst",
                        "Finance Lead",
                        "Compliance Officer",
                        "Operations Head",
                    ],
                    placeholder = "Select designation",
                    required    = True,
                ),
            ]
        ),
    ),

    UIField(
        key         = "start_date",
        label       = "Start Date",
        type        = "date",
        required    = True,
        placeholder = "YYYY-MM-DD",
        help_text   = "Project start date.",
    ),

    UIField(
        key         = "end_date",
        label       = "End Date",
        type        = "date",
        required    = True,
        placeholder = "YYYY-MM-DD",
        help_text   = "Project end date.",
    ),

    # ── Row 4: 2-column (process + solution) ─────────────────────────────────

    UIField(
        key         = "as_is_processes",
        label       = "As-Is Processes & Challenges",
        type        = "textarea",
        required    = True,
        placeholder = "Describe current processes and their challenges...",
        help_text   = "Current state: existing workflows, tools, and pain points.",
    ),

    UIField(
        key         = "proposed_solution",
        label       = "Proposed Solution Overview",
        type        = "textarea",
        required    = True,
        placeholder = "Describe the proposed solution...",
        help_text   = "The recommended approach or solution design.",
    ),

    # ── Row 5: 2-column (optional fields) ────────────────────────────────────

    UIField(
        key         = "constraints",
        label       = "Constraints & Dependencies",
        type        = "textarea",
        required    = False,
        placeholder = "List key constraints and dependencies...",
        help_text   = "Technical, regulatory, or organisational constraints.",
    ),

    UIField(
        key         = "risks",
        label       = "Risk & Mitigation",
        type        = "textarea",
        required    = False,
        placeholder = "Identify risks and mitigation strategies...",
        help_text   = "Key risks and how they will be mitigated.",
    ),

    # ── Row 6: 2-column (technical + cost/priority) ───────────────────────────

    UIField(
        key         = "technical_landscape",
        label       = "Technical Landscape & Integrations",
        type        = "textarea",
        required    = True,
        placeholder = "Describe systems involved, integration requirements, data sources...",
        help_text   = "All systems, APIs, databases, and tech stack involved.",
    ),

    UIField(
        key         = "estimated_cost_crores",
        label       = "Project Estimated Cost (₹ Crores)",
        type        = "number",
        required    = False,
        placeholder = "Enter amount in Crores",
        help_text   = "Budget estimate in Indian Rupees (Crores). e.g. 12.5",
    ),

    UIField(
        key         = "business_priority",
        label       = "Business Priority & Criticality",
        type        = "select",
        required    = True,
        options     = [
            "Critical",
            "Highly Critical",
            "Non-Critical",
        ],
        help_text   = "How critical is this project to business operations.",
    ),

    # ── Generation settings (Step 2) ──────────────────────────────────────────

    UIField(
        key         = "document_type",
        label       = "Document Type",
        type        = "select",
        required    = False,
        options     = [
            "BRD",
            "RFP",
            "SOW",
            "Project Proposal",
            "Technical Specification",
            "Scope Document",
        ],
        default     = "BRD",
        help_text   = "The type of document the AI should generate.",
    ),

    UIField(
        key         = "output_format",
        label       = "Output Format",
        type        = "select",
        required    = False,
        options     = ["Word (.docx)", "PDF", "Markdown"],
        default     = "Word (.docx)",
    ),

    UIField(
        key         = "additional_instructions",
        label       = "Additional Instructions",
        type        = "textarea",
        required    = False,
        placeholder = "e.g. Focus on security requirements. Use formal tone.",
        help_text   = "Free-text instructions passed directly to the AI.",
    ),

    UIField(
        key         = "template_id",
        label       = "Document Template",
        type        = "select",
        required    = False,
        options     = [],   # populated dynamically from DB at runtime
        help_text   = "Optional. Select a saved template to pre-fill section instructions.",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Legacy validated request model (used by POST /api/submit-inputs)
# New flow: use POST /api/projects with ProjectFormData instead.
# ─────────────────────────────────────────────────────────────────────────────

class UserInputRequest(BaseModel):
    document_id:              str
    project_name:             str
    document_type:            str
    output_format:            str  = "Word (.docx)"
    stakeholders:             Optional[str]       = None
    project_description:      Optional[str]       = None
    business_problem:         Optional[str]       = None
    target_audience:          Optional[str]       = None
    sections_to_include:      Optional[List[str]] = None
    generation_mode:          str  = "Complete (single pass)"
    additional_instructions:  Optional[str]       = None
    language:                 str  = "English"
    template_id:              Optional[str]       = None

    @validator("document_type")
    def validate_doc_type(cls, v):
        allowed = ["BRD", "RFP", "SOW", "Project Proposal",
                   "Technical Specification", "Scope Document"]
        if v not in allowed:
            raise ValueError(f"document_type must be one of: {allowed}")
        return v
