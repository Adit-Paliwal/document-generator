"""
ContextCollectorAgent — Agent 2
================================
Loads and presents the project context saved via the frontend form.

Responsibility:
  The frontend flow (Upload → Extract → Fill Form → Save) produces a
  saved project in the DB with two layers of data:
    1. Ingested  — what the user typed into the form
    2. Derived   — what AI extracted from the uploaded documents

  This agent reads BOTH layers, validates completeness, and presents
  a unified context to the user and to the DocumentGeneratorAgent.

  Future extension: "style and tone from similar past documents" will
  be added here as a third context layer (similarity search).

Model: Gemini 2.5 Flash (Google Vertex AI / Gemini API).
       Controlled by agents/_model.py + Data_Ingestion/.env.
"""

from __future__ import annotations
from google.adk.agents import LlmAgent
from .._model          import get_agent_model

from .tools import (
    load_project_context,
    validate_context,
    get_generation_context,
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent instruction
# ─────────────────────────────────────────────────────────────────────────────

_INSTRUCTION = """
You are the Context Collector specialist for IntelliDraft.

Your job is to load, validate, and present the project context that was
saved via the frontend Create Project form. You bridge the frontend data
entry step with the document generation step.

WHAT YOU KNOW:
  The user has already:
  1. Uploaded source documents (parsed by DocParserAgent)
  2. Clicked "Extract" to auto-fill the form from those documents
  3. Reviewed and edited the form fields
  4. Saved the project (which gives a project_id)

  The saved project has TWO layers of context:
    - Ingested data: user-entered fields (project name, problem statement,
      stakeholders, timeline, business unit, etc.)
    - Derived data: AI-extracted fields from the documents (functional
      requirements, success criteria, workflow, systems involved, etc.)

WORKFLOW:
  1. When given a project_id, call load_project_context to fetch both layers.
  2. Call validate_context to check completeness.
     - If critical fields are missing, tell the user which ones and ask them
       to complete the form before generating.
     - If no source documents are attached, tell the user to go back and
       upload a document first.
  3. Call get_generation_context to show the user a clear summary of what
     will be used for generation.
  4. Confirm to the user that the context is ready and they can proceed
     to document generation.

IMPORTANT:
  - You do NOT collect information from the user via conversation.
    The form already collected it. You only LOAD what was saved.
  - You do NOT generate documents. That is DocumentGeneratorAgent's job.
  - The context you load is automatically saved to session state and
    will be available to DocumentGeneratorAgent without re-fetching.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Agent definition
# ─────────────────────────────────────────────────────────────────────────────

context_collector_agent = LlmAgent(
    name        = "ContextCollectorAgent",
    model       = get_agent_model("ContextCollectorAgent"),
    description = (
        "Loads the project context (user-entered form data + AI-derived fields) "
        "saved via the frontend. Validates that all required fields are present "
        "and packages the context for document generation. "
        "Call this agent when the user wants to review their project context "
        "or before starting document generation."
    ),
    instruction = _INSTRUCTION,
    tools       = [
        load_project_context,
        validate_context,
        get_generation_context,
    ],
)
