"""
DocumentGenerator Tools
========================
ADK tools for Agent 3 — DocumentGeneratorAgent.

All tools are thin wrappers over the existing application services.
NO business logic lives here — everything delegates to:
  generation.generation_service  — job lifecycle, sections, comments, regeneration
  generation.doc_writer          — document assembly and export

Tools:
  start_generation      — start generation for a saved project
  get_job_status        — poll generation progress
  list_sections         — list sections with status + content preview
  get_section_content   — full markdown content of one section
  modify_section        — regenerate ONE section with user instruction (chatbot core)
  export_document       — assemble + export the final document
"""

from __future__ import annotations
import logging
import sys
from pathlib import Path

# Ensure Data_Ingestion/ is on sys.path
_BASE = Path(__file__).parent.parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from google.adk.tools import ToolContext  # noqa: E402

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — start_generation
# ─────────────────────────────────────────────────────────────────────────────

async def start_generation(
    project_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Start document generation for a saved project.

    Reads project fields (user-entered + AI-derived) from the database and
    kicks off section-by-section generation in the background.
    Use get_job_status to poll progress.

    Args:
        project_id: UUID of the saved project (must have source docs attached).

    Returns:
        dict with job_id, status, section list, and message.
    """
    try:
        from generation.generation_service import start_job_from_project

        job = start_job_from_project(project_id)

        # Persist IDs in session state so follow-up tools don't need them as args
        tool_context.state["job_id"]     = job["job_id"]
        tool_context.state["project_id"] = project_id

        sections_summary = [
            {
                "section_id": s["section_id"],
                "title":      s["section_title"],
                "status":     s["status"],
            }
            for s in job.get("sections", [])
        ]

        return {
            "job_id":   job["job_id"],
            "status":   job["status"],
            "sections": sections_summary,
            "total":    job.get("total_sections", 0),
            "message": (
                f"Generation started. "
                f"{job.get('total_sections', 0)} sections queued. "
                f"job_id: {job['job_id']}. "
                "Use get_job_status to check progress."
            ),
        }

    except FileNotFoundError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("start_generation failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — get_job_status
# ─────────────────────────────────────────────────────────────────────────────

async def get_job_status(
    job_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Poll the status of a running or completed generation job.

    Args:
        job_id: UUID returned by start_generation.

    Returns:
        dict with overall status, completed/total counts, and per-section status.
    """
    try:
        from generation.generation_service import get_job

        job = get_job(job_id)

        sections_status = [
            {
                "section_id": s["section_id"],
                "title":      s["section_title"],
                "status":     s["status"],
                "version":    s.get("current_version", 0),
            }
            for s in job.get("sections", [])
        ]

        completed = job.get("completed_sections", 0)
        total     = job.get("total_sections", 0)
        status    = job.get("status", "unknown")

        msg_map = {
            "pending":     f"Queued. 0/{total} sections started.",
            "in_progress": f"Generating… {completed}/{total} sections done.",
            "completed":   f"All {total} sections generated successfully.",
            "failed":      f"Generation failed. {completed}/{total} sections completed.",
        }

        return {
            "job_id":    job_id,
            "status":    status,
            "completed": completed,
            "total":     total,
            "sections":  sections_status,
            "message":   msg_map.get(status, f"Status: {status}"),
        }

    except Exception as e:
        logger.exception("get_job_status failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — list_sections
# ─────────────────────────────────────────────────────────────────────────────

async def list_sections(
    job_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    List all sections of a generation job with their current status,
    word count, and a short content preview.

    Args:
        job_id: UUID of the generation job.

    Returns:
        dict with 'sections' list — each has id, title, status, word_count, preview.
    """
    try:
        from generation.generation_service import get_job

        job      = get_job(job_id)
        sections = []

        for s in job.get("sections", []):
            current_v = s.get("current_version", 0)
            versions  = s.get("versions", [])
            latest    = next(
                (v for v in versions if v["version_number"] == current_v), None
            )
            content = latest["content"] if latest else ""
            preview = content[:200].strip() + ("…" if len(content) > 200 else "")

            sections.append({
                "section_id": s["section_id"],
                "title":      s["section_title"],
                "status":     s["status"],
                "version":    current_v,
                "word_count": latest["word_count"] if latest else 0,
                "preview":    preview,
            })

        return {
            "job_id":   job_id,
            "status":   job.get("status"),
            "sections": sections,
            "message":  f"{len(sections)} sections in this job.",
        }

    except Exception as e:
        logger.exception("list_sections failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — get_section_content
# ─────────────────────────────────────────────────────────────────────────────

async def get_section_content(
    section_id: str,
    tool_context: ToolContext,
) -> dict:
    """
    Get the full markdown content of a specific section (latest version).

    Args:
        section_id: UUID of the section (from list_sections).

    Returns:
        dict with section title, version number, word count, and full content.
    """
    try:
        from generation.generation_service import get_section

        sec       = get_section(section_id)
        current_v = sec.get("current_version", 0)
        versions  = sec.get("versions", [])
        latest    = next((v for v in versions if v["version_number"] == current_v), None)

        return {
            "section_id":        section_id,
            "title":             sec["section_title"],
            "status":            sec["status"],
            "version":           current_v,
            "word_count":        latest["word_count"] if latest else 0,
            "content":           latest["content"]    if latest else "",
            "versions_available": len(versions),
        }

    except Exception as e:
        logger.exception("get_section_content failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — modify_section  ← THE CHATBOT CORE
# ─────────────────────────────────────────────────────────────────────────────

async def modify_section(
    section_id:   str,
    instruction:  str,
    tool_context: ToolContext,
) -> dict:
    """
    Regenerate ONE section based on the user's natural-language instruction.

    This is the chatbot core. When a user says:
      "Make the Executive Summary shorter and less technical"
      "Add a risk table to the Risk Register section"
      "Rewrite the Proposed Solution with more emphasis on cost savings"

    ONLY that section is regenerated — the rest of the document is untouched.

    Saves the instruction as a SectionComment (edit_request), then calls
    regenerate_section() which uses the comment as the LLM edit_comment,
    producing a new SectionVersion with an incremented version number.

    Args:
        section_id:  UUID of the section to modify.
        instruction: The user's natural-language modification instruction.

    Returns:
        dict with the new section content, version number, and word count.
    """
    try:
        from generation.generation_service import add_comment, regenerate_section

        # Save the user's instruction as a comment on the section
        comment    = add_comment(section_id, instruction, "edit_request")
        comment_id = comment["comment_id"]

        # Regenerate only this section, incorporating the comment
        new_version = regenerate_section(section_id, comment_id)

        return {
            "section_id":  section_id,
            "version":     new_version["version_number"],
            "word_count":  new_version["word_count"],
            "content":     new_version["content"],
            "instruction": instruction,
            "message": (
                f"Section updated to version {new_version['version_number']} "
                f"({new_version['word_count']} words). "
                "The rest of the document was not changed."
            ),
        }

    except Exception as e:
        logger.exception("modify_section failed")
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 6 — export_document
# ─────────────────────────────────────────────────────────────────────────────

async def export_document(
    job_id:        str,
    output_format: str,
    tool_context:  ToolContext,
) -> dict:
    """
    Assemble and export the final document.

    Collects all sections (using the latest accepted/current version of each)
    and writes the output file.

    Args:
        job_id:        UUID of the completed generation job.
        output_format: 'Word (.docx)', 'PDF', or 'Markdown'.

    Returns:
        dict with file_path, word_count, sections exported, and message.
    """
    try:
        # doc_writer.export_job is the single export entry point used by
        # the Flask API (run_server.py /api/generate/<job_id>/export) too.
        from generation.doc_writer import export_job
        from generation.generation_service import get_job

        file_path, mime_type = export_job(job_id, output_format)

        # Compute totals from the job for the response
        job           = get_job(job_id)
        sections_done = sum(
            1 for s in job.get("sections", []) if s.get("status") == "completed"
        )
        total_words   = sum(
            s.get("current_version", 0) and
            next(
                (v["word_count"] for v in s.get("versions", [])
                 if v["version_number"] == s["current_version"]),
                0,
            )
            for s in job.get("sections", [])
        )

        return {
            "job_id":           job_id,
            "output_format":    output_format,
            "file_path":        str(file_path),
            "mime_type":        mime_type,
            "sections_exported": sections_done,
            "word_count":       total_words,
            "message": (
                f"Document exported as {output_format}. "
                f"{sections_done} sections, saved to: {file_path.name}"
            ),
        }

    except Exception as e:
        logger.exception("export_document failed")
        return {"error": f"{type(e).__name__}: {e}"}
