"""
Project Form Data Schema
========================
Pydantic models that mirror the 'Create New Project' UI form (Steps 1 & 2).

ProjectFormData  — all fields from the intake form
Project          — saved project with metadata + generation job link
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class StakeholderEntry(BaseModel):
    """A single stakeholder row: name + designation."""
    name:        str = ""
    designation: str = ""


class ProjectFormData(BaseModel):
    """
    All fields captured across the Create New Project wizard.

    REQUIRED (marked * in Figma):
      business_unit, project_name, project_code,
      problem_statement, project_objective,
      stakeholders (min 1), start_date, end_date,
      as_is_processes, proposed_solution,
      technical_landscape, business_priority

    OPTIONAL:
      constraints, risks, estimated_cost_crores,
      document_type, output_format, additional_instructions,
      document_ids, template_id
    """

    # ── Required — Identity ───────────────────────────────────────────────────
    business_unit:     str
    project_name:      str
    project_code:      str

    # ── Required — Core content ───────────────────────────────────────────────
    problem_statement:   str
    project_objective:   str
    as_is_processes:     str   # "As-Is Processes and Challenges"
    proposed_solution:   str   # "Proposed Solution Overview"
    technical_landscape: str   # "Technical Landscape and Integrations"

    # ── Required — Structured fields ──────────────────────────────────────────
    stakeholders:      List[StakeholderEntry]   # min 1 entry enforced below
    start_date:        str                       # ISO date YYYY-MM-DD  (Timeline)
    end_date:          str                       # ISO date YYYY-MM-DD  (Timeline)
    business_priority: str                       # Critical | High | Medium | Low

    # ── Optional content ──────────────────────────────────────────────────────
    constraints:           Optional[str] = None
    risks:                 Optional[str] = None
    estimated_cost_crores: Optional[str] = None  # stored as string e.g. "12.5"

    # ── Generation settings (Step 2) ──────────────────────────────────────────
    document_type:           str           = "BRD"
    output_format:           str           = "Word (.docx)"
    additional_instructions: Optional[str] = None

    # ── Source documents + template ───────────────────────────────────────────
    document_ids: List[str]    = Field(default_factory=list)
    template_id:  Optional[str] = None   # UUID of selected template (Step 2)

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("business_unit", "project_name", "project_code",
                     "problem_statement", "project_objective",
                     "as_is_processes", "proposed_solution",
                     "technical_landscape", "business_priority",
                     "start_date", "end_date")
    @classmethod
    def must_not_be_blank(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"'{info.field_name}' is required and cannot be blank.")
        return v.strip()

    @field_validator("stakeholders")
    @classmethod
    def stakeholders_not_empty(cls, v: List[StakeholderEntry]) -> List[StakeholderEntry]:
        filled = [s for s in v if s.name.strip()]
        if not filled:
            raise ValueError("At least one stakeholder with a name is required.")
        return v


class Project(BaseModel):
    """
    A saved project record.
    Wraps ProjectFormData with metadata and tracks the generation job.
    """
    project_id: str     = Field(default_factory=lambda: str(uuid4()))
    created_at: str     = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str     = Field(default_factory=lambda: datetime.utcnow().isoformat())
    status:     str     = "draft"    # draft | ready | generating | completed
    job_id:     Optional[str] = None  # linked generation job

    form_data: ProjectFormData = Field(default_factory=ProjectFormData)
