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

from pydantic import BaseModel, Field


class StakeholderEntry(BaseModel):
    """A single stakeholder row: name + designation."""
    name:        str = ""
    designation: str = ""


class ProjectFormData(BaseModel):
    """
    All fields captured across the Create New Project wizard.

    Step 1 — Project Details
      business_unit, project_name, project_code,
      problem_statement, project_objective,
      stakeholders, start_date, end_date,
      as_is_processes, proposed_solution,
      constraints, risks,
      technical_landscape, estimated_cost_crores, business_priority

    Step 2 — Generation Settings
      document_type, output_format, additional_instructions
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    business_unit: str = ""
    project_name:  str = ""
    project_code:  str = ""

    # ── Core content (required in UI) ─────────────────────────────────────────
    problem_statement:  str = ""
    project_objective:  str = ""
    as_is_processes:    str = ""
    proposed_solution:  str = ""
    technical_landscape: str = ""

    # ── Optional content ──────────────────────────────────────────────────────
    constraints:           Optional[str] = None
    risks:                 Optional[str] = None
    estimated_cost_crores: Optional[str] = None   # stored as string e.g. "12.5"

    # ── Structured fields ─────────────────────────────────────────────────────
    stakeholders:     List[StakeholderEntry] = Field(default_factory=list)
    start_date:       Optional[str] = None    # ISO date  YYYY-MM-DD
    end_date:         Optional[str] = None
    business_priority: Optional[str] = None   # Critical | High | Medium | Low

    # ── Generation settings (Step 2) ──────────────────────────────────────────
    document_type:           str           = "BRD"
    output_format:           str           = "Word (.docx)"
    additional_instructions: Optional[str] = None

    # ── Source documents + template ───────────────────────────────────────────
    document_ids: List[str]   = Field(default_factory=list)
    template_id:  Optional[str] = None    # UUID of selected template (Step 2)


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
