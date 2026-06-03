"""
UI Input Schema
===============
Defines every field the user fills in via the frontend form.
Each field has:
  - key:         form field name
  - label:       UI display label
  - type:        text | textarea | select | multiselect | checkbox
  - required:    whether the field is mandatory
  - options:     for select/multiselect fields
  - placeholder: hint text shown in the UI
  - help_text:   tooltip/help shown next to the field

The frontend can render these dynamically — no hardcoded form needed.
POST this JSON to /api/submit-inputs to validate and store user inputs.
"""

from __future__ import annotations
from typing import Any, List, Literal, Optional
from pydantic import BaseModel, validator


# ─────────────────────────────────────────────────────────────────────────────
# Field definition (for dynamic UI rendering)
# ─────────────────────────────────────────────────────────────────────────────

class UIField(BaseModel):
    key:         str
    label:       str
    type:        Literal["text", "textarea", "select", "multiselect", "checkbox"]
    required:    bool        = False
    options:     List[str]   = []
    placeholder: str         = ""
    help_text:   str         = ""
    default:     Any         = None


# ─────────────────────────────────────────────────────────────────────────────
# The complete form definition returned to the frontend
# ─────────────────────────────────────────────────────────────────────────────

DOCUMENT_FORM_FIELDS: List[UIField] = [

    UIField(
        key         = "project_name",
        label       = "Project Name",
        type        = "text",
        required    = True,
        placeholder = "e.g. Payment Gateway Integration",
        help_text   = "The name of the project this document is for.",
    ),

    UIField(
        key      = "document_type",
        label    = "Output Document Type",
        type     = "select",
        required = True,
        options  = [
            "Business Requirements Document (BRD)",
            "Scope Document",
            "Project Proposal",
            "Statement of Work (SOW)",
            "Technical Specification",
            "Meeting Minutes",
            "Executive Summary",
            "Custom",
        ],
        help_text = "The type of document the AI should generate.",
    ),

    UIField(
        key      = "output_format",
        label    = "Output Format",
        type     = "select",
        required = True,
        options  = ["Word (.docx)", "PDF", "Markdown"],
        default  = "Word (.docx)",
    ),

    UIField(
        key         = "stakeholders",
        label       = "Stakeholders",
        type        = "text",
        placeholder = "e.g. CTO, Product Manager, Dev Team, Client",
        help_text   = "Comma-separated list of stakeholders.",
    ),

    UIField(
        key         = "project_description",
        label       = "Project Description",
        type        = "textarea",
        placeholder = "Briefly describe what the project does and its goals.",
        help_text   = "This is passed directly to the AI as context.",
    ),

    UIField(
        key         = "business_problem",
        label       = "Business Problem / Opportunity",
        type        = "textarea",
        placeholder = "What problem does this project solve?",
    ),

    UIField(
        key         = "target_audience",
        label       = "Target Audience",
        type        = "text",
        placeholder = "e.g. Internal IT team, End customers, Regulators",
    ),

    UIField(
        key     = "sections_to_include",
        label   = "Sections to Include",
        type    = "multiselect",
        options = [
            "Executive Summary",
            "Business Objectives",
            "Scope",
            "Stakeholders",
            "Functional Requirements",
            "Non-Functional Requirements",
            "Data Requirements",
            "Integration Requirements",
            "User Roles",
            "Assumptions",
            "Constraints",
            "Risks",
            "Acceptance Criteria",
            "Glossary",
        ],
        help_text = "Leave empty to use the default template for the chosen document type.",
    ),

    UIField(
        key      = "generation_mode",
        label    = "Generation Mode",
        type     = "select",
        options  = ["Complete (single pass)", "Section by section"],
        default  = "Complete (single pass)",
        help_text= "Section by section is recommended for large documents (>30 pages).",
    ),

    UIField(
        key         = "additional_instructions",
        label       = "Additional Instructions for the AI",
        type        = "textarea",
        placeholder = "e.g. Focus on security requirements. Use formal tone. Avoid technical jargon.",
        help_text   = "Free-text instructions passed directly to the LLM.",
    ),

    UIField(
        key     = "language",
        label   = "Output Language",
        type    = "select",
        options = ["English", "Hindi", "Gujarati", "Custom"],
        default = "English",
    ),

    UIField(
        key      = "template_id",
        label    = "Document Template",
        type     = "select",
        options  = [],          # populated dynamically from DB at runtime
        help_text= "Optional. Select a saved template to pre-fill section instructions.",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Validated request model (used in POST /api/submit-inputs)
# ─────────────────────────────────────────────────────────────────────────────

class UserInputRequest(BaseModel):
    document_id:              str           # links to the uploaded ParsedDocument
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
        allowed = [f.options for f in DOCUMENT_FORM_FIELDS if f.key == "document_type"][0]
        if v not in allowed:
            raise ValueError(f"document_type must be one of: {allowed}")
        return v
