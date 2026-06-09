"""
Generation Service
==================
Orchestrates the full document generation workflow:

  start_job()             → creates DB records, launches background thread
  add_comment()           → store a user comment on a section version
  regenerate_section()    → re-call LLM for one section (with optional comment)
  accept_version()        → mark a specific version as accepted by the user
  get_job()               → full job state with sections + versions + comments
  export_job()            → assemble final document from accepted/current versions

Background generation (dev mode):
  Sections are generated one-by-one in a daemon thread so the HTTP call
  that started the job returns immediately. The client polls GET /api/generate/{job_id}.

Production note:
  Replace the thread with an Azure Service Bus queue trigger.
  The `_run_generation_job` function is the exact body the queue handler would execute.
  Set ASYNC_GENERATION=false to generate synchronously inside the request (useful for
  testing or very small documents).
"""

from __future__ import annotations
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from generation.db import (
    GenerationJob, Section, SectionVersion, SectionComment,
    get_session,
)
from generation.template_manager import get_sections_for_job, get_template_by_id
from generation.generator import generate_section
from storage.azure_storage import get_storage_service

logger = logging.getLogger(__name__)

# Set ASYNC_GENERATION=false to generate synchronously (blocking) inside the request.
# Default true = background thread, HTTP call returns immediately.
ASYNC_GENERATION = os.environ.get("ASYNC_GENERATION", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Start a generation job
# ─────────────────────────────────────────────────────────────────────────────

def start_job(
    document_id:    str,
    user_inputs:    dict,
    template_id:    Optional[str] = None,
) -> dict:
    """
    Create a GenerationJob and all its Section rows in the DB.
    Start section generation (background thread or sync based on ASYNC_GENERATION).

    Args:
        document_id:  UUID of the parsed document (from the upload step)
        user_inputs:  Dict matching UserInputData fields
        template_id:  Optional template override

    Returns:
        Job dict (with sections in pending state)
    """
    import json as json_mod

    document_type = user_inputs.get("document_type", "Business Requirements Document (BRD)")
    sections_override = user_inputs.get("sections_to_include")

    section_configs = get_sections_for_job(document_type, template_id, sections_override)

    if not section_configs:
        raise ValueError(f"No sections resolved for document_type='{document_type}'")

    job_id = str(uuid.uuid4())

    with get_session() as session:
        job = GenerationJob(
            job_id           = job_id,
            document_id      = document_id,
            status           = "pending",
            document_type    = document_type,
            output_format    = user_inputs.get("output_format", "Word (.docx)"),
            template_id      = template_id,
            language         = user_inputs.get("language", "English"),
            user_inputs_json = json_mod.dumps(user_inputs),
            total_sections   = len(section_configs),
        )
        session.add(job)

        for cfg in section_configs:
            section = Section(
                section_id    = str(uuid.uuid4()),
                job_id        = job_id,
                section_key   = cfg["key"],
                section_title = cfg["title"],
                order_index   = cfg["order"],
                status        = "pending",
            )
            session.add(section)

        session.commit()
        job_dict = job.to_dict()

    logger.info("Created job %s with %d sections", job_id, len(section_configs))

    if ASYNC_GENERATION:
        # Launch background thread — returns immediately
        t = threading.Thread(
            target=_run_generation_job,
            args=(job_id,),
            daemon=True,
            name=f"gen-{job_id[:8]}",
        )
        t.start()
    else:
        # Synchronous — blocks until all sections are done
        _run_generation_job(job_id)
        with get_session() as session:
            job_obj = session.get(GenerationJob, job_id)
            job_dict = job_obj.to_dict()

    return job_dict


# ─────────────────────────────────────────────────────────────────────────────
# Background generation loop
# ─────────────────────────────────────────────────────────────────────────────

def _run_generation_job(job_id: str) -> None:
    """
    Generate all pending sections for a job, sequentially.
    Updates the DB after each section so the client can poll progress.
    This is the body that a production queue-triggered function would execute.
    """
    logger.info("[gen] Starting job %s", job_id)

    # Load document context once for the whole job
    try:
        llm_context, user_inputs, system_instructions = _load_job_context(job_id)
    except Exception as e:
        logger.exception("[gen] Failed to load context for job %s", job_id)
        _mark_job_failed(job_id, str(e))
        return

    # Mark job as in_progress
    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            return
        job.status = "in_progress"
        session.commit()

    # Load section IDs in order
    with get_session() as session:
        sections = (
            session.query(Section)
            .filter(Section.job_id == job_id)
            .order_by(Section.order_index)
            .all()
        )
        section_rows = [(s.section_id, s.section_key, s.section_title, s.order_index) for s in sections]

    # Pull section configs from template for instructions
    document_type     = user_inputs.get("document_type", "")
    template_id       = user_inputs.get("template_id")
    section_configs   = get_sections_for_job(document_type, template_id, None)
    config_by_key     = {c["key"]: c for c in section_configs}

    previous_sections: list[dict] = []   # accumulated for coherence

    for section_id, section_key, section_title, order_index in section_rows:
        cfg          = config_by_key.get(section_key, {})
        instructions = cfg.get("instructions", f"Generate the {section_title} section.")
        target_words = cfg.get("target_words", 300)

        # Mark as generating
        with get_session() as session:
            sec = session.get(Section, section_id)
            if not sec or sec.status not in ("pending",):
                continue   # skip if already done or failed
            sec.status     = "generating"
            sec.updated_at = datetime.utcnow()
            session.commit()

        try:
            content, prompt, model_id = generate_section(
                section_key          = section_key,
                section_title        = section_title,
                section_instructions = instructions,
                document_type        = document_type,
                system_instructions  = system_instructions,
                llm_context          = llm_context,
                user_inputs          = user_inputs,
                previous_sections    = previous_sections,
                target_words         = target_words,
            )

            version_id = str(uuid.uuid4())
            with get_session() as session:
                sec = session.get(Section, section_id)
                ver = SectionVersion(
                    version_id        = version_id,
                    section_id        = section_id,
                    version_number    = 1,
                    content           = content,
                    word_count        = len(content.split()),
                    generation_prompt = prompt,
                    generation_model  = model_id,
                )
                session.add(ver)
                sec.status          = "completed"
                sec.current_version = 1
                sec.updated_at      = datetime.utcnow()

                # Increment job counter
                job = session.get(GenerationJob, job_id)
                job.completed_sections += 1
                session.commit()

            previous_sections.append({"title": section_title, "content": content})
            logger.info("[gen] Section '%s' done (%d words)", section_key, len(content.split()))

        except Exception as e:
            logger.exception("[gen] Section '%s' failed", section_key)
            with get_session() as session:
                sec = session.get(Section, section_id)
                sec.status     = "failed"
                sec.error      = str(e)
                sec.updated_at = datetime.utcnow()
                job = session.get(GenerationJob, job_id)
                job.completed_sections += 1
                session.commit()

    # Final job status — use SQL aggregates to avoid lazy-load DetachedInstanceError
    with get_session() as session:
        total_sections = session.scalar(
            select(func.count()).where(Section.job_id == job_id)
        ) or 0
        failed_sections = session.scalar(
            select(func.count()).where(
                Section.job_id == job_id,
                Section.status == "failed",
            )
        ) or 0
        final_status = "failed" if (failed_sections > 0 and failed_sections == total_sections) else "completed"

        job = session.get(GenerationJob, job_id)
        if job:
            job.status       = final_status
            job.completed_at = datetime.utcnow()

            # Also flip the linked Project status so the dashboard reflects reality.
            # Project lifecycle: draft → ready → generating → completed | failed
            from generation.db import Project as _Project
            linked_proj = session.query(_Project).filter(
                _Project.job_id == job_id
            ).first()
            if linked_proj:
                linked_proj.status     = final_status   # "completed" or "failed"
                linked_proj.updated_at = datetime.utcnow()
                logger.info(
                    "[gen] Project %s status → %s",
                    linked_proj.project_id, final_status,
                )

            session.commit()

    logger.info("[gen] Job %s finished — status=%s (total=%d failed=%d)",
                job_id, final_status, total_sections, failed_sections)


def _load_job_context(job_id: str) -> tuple[str, dict, str]:
    """
    Load the document context and user inputs for a job.
    Returns (llm_context, user_inputs_dict, system_instructions).

    Multi-document support:
      If user_inputs contains "document_ids" (list), all documents are loaded
      and their contexts are concatenated (capped at 60 000 chars total).
      Falls back to the single job.document_id when "document_ids" is absent
      (e.g. jobs started via POST /api/generate/start with a single document_id).
    """
    import json as json_mod

    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        document_id      = job.document_id
        user_inputs_json = job.user_inputs_json
        template_id      = job.template_id
        document_type    = job.document_type

    user_inputs = json_mod.loads(user_inputs_json or "{}")

    # ── Resolve document ID list ──────────────────────────────────────────────
    # generate_from_project passes all attached doc IDs via user_inputs["document_ids"].
    # generate_start passes a single document_id at the job level.
    all_doc_ids: list[str] = user_inputs.get("document_ids") or [document_id]

    # ── Load and concatenate document contexts ────────────────────────────────
    store = get_storage_service()
    from models.meta_schema import ParsedDocument

    contexts: list[str] = []
    for doc_id in all_doc_ids:
        try:
            meta      = store.get_meta_json(doc_id)
            parsed    = ParsedDocument(**meta)
            # Cap per-document to 20 000 chars; total capped below at 60 000.
            ctx       = parsed.to_llm_context(max_chars=20_000)
            header    = f"=== Source Document: {parsed.source_filename} ===" if len(all_doc_ids) > 1 else ""
            contexts.append(f"{header}\n{ctx}".strip())
            logger.info("[gen] Loaded doc %s (%d chars)", parsed.source_filename, len(ctx))
        except Exception as e:
            logger.warning("[gen] Could not load document %s: %s", doc_id, e)

    if not contexts:
        raise FileNotFoundError(
            f"No documents could be loaded for job {job_id}. "
            f"Checked document IDs: {all_doc_ids}"
        )

    llm_context = "\n\n---\n\n".join(contexts)[:60_000]

    # ── Get template system instructions ─────────────────────────────────────
    # template_id is stored on the job record (set by start_job).
    # For generate_from_project it's also in user_inputs["template_id"] so
    # _run_generation_job can resolve sections correctly.
    system_instructions = ""
    if template_id:
        tmpl = get_template_by_id(template_id)
        if tmpl and tmpl.system_instructions:
            system_instructions = tmpl.system_instructions
    if not system_instructions:
        from generation.template_manager import get_template_for_doc_type
        tmpl = get_template_for_doc_type(document_type)
        if tmpl and tmpl.system_instructions:
            system_instructions = tmpl.system_instructions

    return llm_context, user_inputs, system_instructions


def _mark_job_failed(job_id: str, error: str) -> None:
    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if job:
            job.status       = "failed"
            job.error        = error
            job.completed_at = datetime.utcnow()
            session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# User actions (comments, regeneration, acceptance)
# ─────────────────────────────────────────────────────────────────────────────

def add_comment(
    section_id:   str,
    comment_text: str,
    comment_type: str = "edit_request",
) -> dict:
    """Add a user comment on a section. Returns the saved comment dict."""
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        comment = SectionComment(
            comment_id     = str(uuid.uuid4()),
            section_id     = section_id,
            version_number = sec.current_version,
            comment_text   = comment_text,
            comment_type   = comment_type,
            status         = "pending",
        )
        session.add(comment)
        session.commit()
        session.refresh(comment)
        return comment.to_dict()


def regenerate_section(
    section_id:  str,
    comment_id:  Optional[str] = None,
) -> dict:
    """
    Regenerate a section. If comment_id is given, incorporates that comment.
    Creates a new SectionVersion with an incremented version_number.
    Returns the new version dict.
    """
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        job = session.get(GenerationJob, sec.job_id)
        if not job:
            raise ValueError(f"Job for section {section_id} not found")

        # Get current content to revise
        current_ver = None
        if sec.versions:
            current_ver = max(sec.versions, key=lambda v: v.version_number)

        # Get the edit comment if specified
        edit_comment     = None
        trigger_cmt_id   = None
        if comment_id:
            cmt = session.get(SectionComment, comment_id)
            if cmt:
                edit_comment   = cmt.comment_text
                trigger_cmt_id = comment_id

        next_version = (sec.current_version or 0) + 1

        # Snapshot values before closing session
        section_key     = sec.section_key
        section_title   = sec.section_title
        job_id          = sec.job_id
        document_type   = job.document_type
        template_id     = job.template_id
        current_content = current_ver.content if current_ver else None
        user_inputs_json = job.user_inputs_json

        # Mark section as regenerating
        sec.status     = "generating"
        sec.updated_at = datetime.utcnow()
        session.commit()

    # Load full context
    import json as json_mod
    user_inputs = json_mod.loads(user_inputs_json or "{}")
    llm_context, _, system_instructions = _load_job_context(job_id)

    # Section-specific instructions
    section_configs = get_sections_for_job(document_type, template_id, None)
    config_by_key   = {c["key"]: c for c in section_configs}
    cfg             = config_by_key.get(section_key, {})
    instructions    = cfg.get("instructions", f"Generate the {section_title} section.")
    target_words    = cfg.get("target_words", 300)

    # Previously completed sections for coherence
    with get_session() as session:
        all_sections = (
            session.query(Section)
            .filter(Section.job_id == job_id, Section.status == "completed")
            .order_by(Section.order_index)
            .all()
        )
        prev_sections = []
        for s in all_sections:
            if s.section_id == section_id:
                continue
            latest = max(s.versions, key=lambda v: v.version_number) if s.versions else None
            if latest:
                prev_sections.append({"title": s.section_title, "content": latest.content})

    try:
        content, prompt, model_id = generate_section(
            section_key          = section_key,
            section_title        = section_title,
            section_instructions = instructions,
            document_type        = document_type,
            system_instructions  = system_instructions,
            llm_context          = llm_context,
            user_inputs          = user_inputs,
            previous_sections    = prev_sections,
            target_words         = target_words,
            edit_comment         = edit_comment,
            previous_content     = current_content,
        )

        with get_session() as session:
            ver = SectionVersion(
                version_id        = str(uuid.uuid4()),
                section_id        = section_id,
                version_number    = next_version,
                content           = content,
                word_count        = len(content.split()),
                generation_prompt = prompt,
                generation_model  = model_id,
                trigger_comment_id= trigger_cmt_id,
            )
            session.add(ver)

            sec = session.get(Section, section_id)
            sec.status          = "completed"
            sec.current_version = next_version
            sec.updated_at      = datetime.utcnow()

            # Mark the triggering comment as addressed
            if comment_id:
                cmt = session.get(SectionComment, comment_id)
                if cmt:
                    cmt.status = "addressed"

            session.commit()
            session.refresh(ver)
            return ver.to_dict()

    except Exception as e:
        with get_session() as session:
            sec = session.get(Section, section_id)
            if sec:
                sec.status     = "failed"
                sec.error      = str(e)
                sec.updated_at = datetime.utcnow()
                session.commit()
        raise


def accept_version(section_id: str, version_number: int) -> dict:
    """
    Mark a specific version as accepted by the user.
    Clears the is_accepted flag on all other versions of the same section.
    Updates section.current_version to the accepted version.
    """
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")

        for ver in sec.versions:
            ver.is_accepted = (ver.version_number == version_number)

        sec.current_version = version_number
        sec.updated_at      = datetime.utcnow()
        session.commit()

        accepted = next((v for v in sec.versions if v.version_number == version_number), None)
        return accepted.to_dict() if accepted else {}


# ─────────────────────────────────────────────────────────────────────────────
# Read queries
# ─────────────────────────────────────────────────────────────────────────────

def get_job(job_id: str) -> dict:
    """Return full job state including all sections, versions, and comments."""
    with get_session() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        return job.to_dict(include_sections=True)


def get_section(section_id: str) -> dict:
    """Return a single section with all versions and comments."""
    with get_session() as session:
        sec = session.get(Section, section_id)
        if not sec:
            raise ValueError(f"Section {section_id} not found")
        return sec.to_dict(include_versions=True, include_comments=True)


def list_jobs(document_id: Optional[str] = None) -> list[dict]:
    """List all jobs, optionally filtered by document_id."""
    with get_session() as session:
        q = session.query(GenerationJob).order_by(GenerationJob.created_at.desc())
        if document_id:
            q = q.filter(GenerationJob.document_id == document_id)
        return [j.to_dict(include_sections=False) for j in q.all()]
