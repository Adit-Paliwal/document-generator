"""
Azure Functions Backend — Intellidraft API
==========================================
Ingestion endpoints:
  GET  /api/form-fields                              → dynamic form field definitions
  POST /api/upload                                   → upload + parse document
  POST /api/submit-inputs                            → attach user context to a parsed doc
  GET  /api/document/{id}                            → fetch full ParsedDocument meta JSON
  GET  /api/document/{id}/status                     → lightweight status

Generation endpoints:
  POST /api/generate/start                           → create job + start background generation
  GET  /api/generate/{job_id}                        → poll job status + all sections
  GET  /api/generate/{job_id}/section/{section_id}   → single section with all versions + comments
  POST /api/generate/{job_id}/section/{section_id}/comment    → add user edit request
  POST /api/generate/{job_id}/section/{section_id}/regenerate → regenerate section (w/ optional comment)
  POST /api/generate/{job_id}/section/{section_id}/accept     → accept a specific version
  GET  /api/generate/{job_id}/export                 → download assembled document

Template endpoints:
  GET  /api/templates                                → list available templates
  POST /api/templates                                → create a user template

Production notes:
  - store and DB are lazily initialised (avoids import-time crash when env vars absent)
  - All responses include CORS headers so the React frontend can call from localhost
  - File uploads capped at 50 MB
"""

import azure.functions as func
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from parsers.parser_factory import parse_document, SUPPORTED_EXTENSIONS
from storage.azure_storage  import get_storage_service
from api.user_input_schema  import DOCUMENT_FORM_FIELDS, UserInputRequest
from models.meta_schema     import UserInputData, ParsedDocument

app    = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB

# Lazy singleton — avoids crashing on cold start when env vars are absent
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = get_storage_service()
    return _store


# ─────────────────────────────────────────────────────────────────────────────
# 1. GET /api/form-fields
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="form-fields", methods=["GET"])
def get_form_fields(req: func.HttpRequest) -> func.HttpResponse:
    fields = [f.model_dump() for f in DOCUMENT_FORM_FIELDS]
    return _json({"fields": fields})


# ─────────────────────────────────────────────────────────────────────────────
# 2. POST /api/upload
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="upload", methods=["POST"])
def upload_document(req: func.HttpRequest) -> func.HttpResponse:
    try:
        file_data = req.files.get("file")
        if not file_data:
            return _json({"error": "No file provided. Send as multipart field 'file'."}, 400)

        filename = file_data.filename or "upload"
        ext      = Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            return _json({
                "error":     f"Unsupported file type '{ext}'.",
                "supported": SUPPORTED_EXTENSIONS,
            }, 415)

        raw = file_data.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return _json({
                "error": f"File too large ({len(raw) // (1024*1024)} MB). Maximum is 50 MB."
            }, 413)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)

        logger.info("Parsing uploaded file: %s (%d bytes)", filename, len(raw))

        try:
            parsed_doc = parse_document(tmp_path)
            parsed_doc.source_filename = filename
            parsed_doc = _get_store().persist_all(parsed_doc, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        return _json({
            "document_id":  parsed_doc.document_id,
            "filename":     filename,
            "file_type":    parsed_doc.file_type,
            "blob_base":    parsed_doc.blob_base_path,
            "summary":      parsed_doc.summary.model_dump(),
            "message":      (
                f"Document parsed successfully. "
                f"Found {parsed_doc.summary.total_text_elements} text blocks, "
                f"{parsed_doc.summary.total_images} images "
                f"({parsed_doc.summary.images_analyzed} analysed by AI), "
                f"{parsed_doc.summary.total_tables} tables."
            ),
        }, 201)

    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("Upload failed")
        return _json({"error": "Parsing failed. Check server logs for details."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 3. POST /api/submit-inputs
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="submit-inputs", methods=["POST"])
def submit_user_inputs(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body     = req.get_json()
        req_data = UserInputRequest(**body)

        meta        = _get_store().get_meta_json(req_data.document_id)
        updated_doc = ParsedDocument(**meta)
        updated_doc.user_inputs = UserInputData(
            **req_data.model_dump(exclude={"document_id"})
        )

        _get_store().save_meta_json(updated_doc)
        _get_store().save_to_cosmos(updated_doc)

        return _json({
            "document_id": req_data.document_id,
            "message":     "User inputs saved. Ready for document generation.",
        })

    except FileNotFoundError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("submit-inputs failed")
        return _json({"error": "Failed to save inputs. Check server logs."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 4. GET /api/document/{id}
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="document/{doc_id}", methods=["GET"])
def get_document(req: func.HttpRequest) -> func.HttpResponse:
    doc_id = req.route_params.get("doc_id")
    try:
        meta = _get_store().get_meta_json(doc_id)
        return _json(meta)
    except FileNotFoundError:
        return _json({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        logger.exception("get_document failed")
        return _json({"error": "Failed to retrieve document."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 5. GET /api/document/{id}/status
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="document/{doc_id}/status", methods=["GET"])
def get_document_status(req: func.HttpRequest) -> func.HttpResponse:
    doc_id = req.route_params.get("doc_id")
    try:
        record = _get_store().get_document_index(doc_id)
        return _json(record)
    except FileNotFoundError:
        return _json({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        logger.exception("get_document_status failed")
        return _json({"error": "Failed to retrieve status."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _json(data: dict, status: int = 200) -> func.HttpResponse:
    """Return a JSON response with CORS headers so the React UI can call from any origin."""
    return func.HttpResponse(
        body        = json.dumps(data, default=str),
        status_code = status,
        mimetype    = "application/json",
        headers     = {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# GENERATION ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# 6. POST /api/generate/start  [DEPRECATED]
# Use POST /api/generate/project/{project_id} instead.
# This legacy endpoint requires a raw document_id and bypasses the project DB.
# Kept for backwards compatibility only.
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/start", methods=["POST"])
def generate_start(req: func.HttpRequest) -> func.HttpResponse:
    """
    DEPRECATED — use POST /api/generate/project/{project_id}.
    Body: { document_id, user_inputs: {...}, template_id? }
    """
    try:
        body        = req.get_json()
        document_id = body.get("document_id")
        user_inputs = body.get("user_inputs") or {}
        template_id = body.get("template_id")

        if not document_id:
            return _json({"error": "document_id is required"}, 400)
        if not user_inputs.get("document_type"):
            return _json({"error": "user_inputs.document_type is required"}, 400)

        from generation.generation_service import start_job
        job = start_job(document_id, user_inputs, template_id)

        resp = _json({
            "job_id":    job["job_id"],
            "status":    job["status"],
            "sections":  [
                {"section_id": s["section_id"], "section_title": s["section_title"],
                 "status": s["status"]}
                for s in job.get("sections", [])
            ],
            "message": (
                f"Generation job started. {job['total_sections']} sections queued. "
                "Poll GET /api/generate/{job_id} for progress."
            ),
            "deprecated": "Use POST /api/generate/project/{project_id} instead.",
        }, 201)
        resp.headers["X-Deprecated"] = "Use POST /api/generate/project/{project_id} — this endpoint will be removed in a future release"
        return resp

    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate/start failed")
        return _json({"error": "Failed to start generation job."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 7. GET /api/generate/{job_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}", methods=["GET"])
def generate_get_job(req: func.HttpRequest) -> func.HttpResponse:
    """Poll job status. Returns full job with all sections + latest version content."""
    job_id = req.route_params.get("job_id")
    try:
        from generation.generation_service import get_job
        job = get_job(job_id)

        # Slim down version history in the list view — keep only current version content
        for sec in job.get("sections", []):
            current_v = sec.get("current_version", 0)
            versions  = sec.get("versions", [])
            current   = next((v for v in versions if v["version_number"] == current_v), None)
            sec["current_content"] = current["content"] if current else None
            sec["version_count"]   = len(versions)
            sec.pop("versions", None)   # remove full list to keep response lean

        return _json(job)
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_get_job failed")
        return _json({"error": "Failed to retrieve job."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 8. GET /api/generate/{job_id}/section/{section_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/section/{section_id}", methods=["GET"])
def generate_get_section(req: func.HttpRequest) -> func.HttpResponse:
    """Return a single section with ALL versions and ALL comments."""
    section_id = req.route_params.get("section_id")
    try:
        from generation.generation_service import get_section
        return _json(get_section(section_id))
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_get_section failed")
        return _json({"error": "Failed to retrieve section."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 9. POST /api/generate/{job_id}/section/{section_id}/comment
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/section/{section_id}/comment", methods=["POST"])
def generate_add_comment(req: func.HttpRequest) -> func.HttpResponse:
    """
    Add a user comment (edit request) to a section.
    Body: { comment_text, comment_type? }
    comment_type: edit_request (default) | approval | rejection | note
    """
    section_id = req.route_params.get("section_id")
    try:
        body         = req.get_json()
        comment_text = body.get("comment_text", "").strip()
        comment_type = body.get("comment_type", "edit_request")

        if not comment_text:
            return _json({"error": "comment_text is required"}, 400)

        from generation.generation_service import add_comment
        comment = add_comment(section_id, comment_text, comment_type)
        return _json({"comment": comment, "message": "Comment saved."}, 201)

    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_add_comment failed")
        return _json({"error": "Failed to save comment."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 10. POST /api/generate/{job_id}/section/{section_id}/regenerate
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/section/{section_id}/regenerate", methods=["POST"])
def generate_regenerate_section(req: func.HttpRequest) -> func.HttpResponse:
    """
    Regenerate a section, optionally incorporating a specific comment.
    Body: { comment_id? }
    If comment_id is omitted, regenerates the section from scratch (no edit instruction).
    """
    section_id = req.route_params.get("section_id")
    try:
        body       = req.get_json() or {}
        comment_id = body.get("comment_id")

        from generation.generation_service import regenerate_section
        new_version = regenerate_section(section_id, comment_id)
        return _json({
            "new_version": new_version,
            "message":     f"Section regenerated — version {new_version['version_number']}.",
        })

    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_regenerate_section failed")
        return _json({"error": "Regeneration failed. Check server logs."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 11. POST /api/generate/{job_id}/section/{section_id}/accept
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/section/{section_id}/accept", methods=["POST"])
def generate_accept_version(req: func.HttpRequest) -> func.HttpResponse:
    """
    Mark a specific version of a section as accepted.
    Body: { version_number }
    """
    section_id = req.route_params.get("section_id")
    try:
        body           = req.get_json()
        version_number = body.get("version_number")
        if version_number is None:
            return _json({"error": "version_number is required"}, 400)

        from generation.generation_service import accept_version
        accepted = accept_version(section_id, int(version_number))
        return _json({
            "accepted_version": accepted,
            "message": f"Version {version_number} accepted.",
        })

    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_accept_version failed")
        return _json({"error": "Failed to accept version."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 12a. PATCH /api/generate/{job_id}/section/{section_id}
#      Manual content override (inline editor in the preview panel)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/section/{section_id}", methods=["PATCH"])
def generate_update_section(req: func.HttpRequest) -> func.HttpResponse:
    """
    Directly overwrite a section's content with manually-edited text.
    Body: { "content": "..." }
    Creates a new version marked as accepted.
    """
    section_id = req.route_params.get("section_id")
    try:
        body    = req.get_json() or {}
        content = body.get("content", "").strip()
        if not content:
            return _json({"error": "content is required"}, 400)

        from generation.generation_service import update_section_content
        new_version = update_section_content(section_id, content)
        return _json({
            "version": new_version,
            "message": f"Section updated — version {new_version['version_number']}.",
        })

    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_update_section failed")
        return _json({"error": "Failed to update section."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 12. GET /api/generate/{job_id}/preview
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/preview", methods=["GET"])
def generate_preview(req: func.HttpRequest) -> func.HttpResponse:
    """
    Return the assembled document as markdown for on-screen rendering.
    No file download — returns JSON with sections and full markdown string.
    Works in both local and cloud (Azure Blob) mode.

    Response: { job_id, status, document_type, project_name,
                sections: [{order, title, content, status, word_count}],
                markdown, export_urls, blob_url }
    """
    job_id = req.route_params.get("job_id")
    try:
        from generation.doc_writer import assemble_preview
        return _json(assemble_preview(job_id))
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_preview failed")
        return _json({"error": f"Preview failed: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 12c. GET /api/generate/{job_id}/preview/html  — LibreOffice async preview
# 12d. GET /api/generate/{job_id}/preview/status — poll Celery task
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/preview/html", methods=["GET"])
def generate_preview_html(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns LibreOffice-rendered HTML for the document.
    CELERY_ENABLED=true → async (202 + task_id on cache miss).
    CELERY_ENABLED=false → synchronous fallback (200 + html).
    """
    job_id = req.route_params.get("job_id")
    try:
        from generation.preview_service import get_or_submit_preview
        result = get_or_submit_preview(job_id)
        status_code = 200 if result.get("status") == "ready" else (
            202 if result.get("status") == "pending" else 500
        )
        return _json(result, status_code)
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_preview_html failed")
        return _json({"error": str(e)}, 500)


@app.route(route="generate/{job_id}/preview/status", methods=["GET"])
def generate_preview_status(req: func.HttpRequest) -> func.HttpResponse:
    """Poll a Celery conversion task by task_id."""
    job_id  = req.route_params.get("job_id")
    task_id = req.params.get("task_id", "").strip()
    if not task_id:
        return _json({"error": "task_id query param required"}, 400)
    try:
        from generation.preview_service import poll_preview_status
        result = poll_preview_status(job_id, task_id)
        status_code = 200 if result.get("status") == "ready" else (
            202 if result.get("status") == "pending" else 500
        )
        return _json(result, status_code)
    except Exception as e:
        logger.exception("generate_preview_status failed")
        return _json({"error": str(e)}, 500)


# 12e. POST /api/generate/{job_id}/snapshot — create a version checkpoint
@app.route(route="generate/{job_id}/snapshot", methods=["POST"])
def generate_create_snapshot(req: func.HttpRequest) -> func.HttpResponse:
    job_id = req.route_params.get("job_id")
    try:
        body         = req.get_json(silent=True) or {}
        label        = (body.get("label") or "").strip()
        trigger_type = (body.get("trigger_type") or "manual").strip()
        from generation.generation_service import create_snapshot
        snap = create_snapshot(job_id, label, trigger_type)
        return _json(snap, 201)
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("create_snapshot failed")
        return _json({"error": str(e)}, 500)


# 12f. GET /api/generate/{job_id}/snapshots — list version history
@app.route(route="generate/{job_id}/snapshots", methods=["GET"])
def generate_list_snapshots(req: func.HttpRequest) -> func.HttpResponse:
    job_id = req.route_params.get("job_id")
    try:
        from generation.generation_service import list_snapshots
        snaps = list_snapshots(job_id)
        return _json({"snapshots": snaps})
    except Exception as e:
        logger.exception("list_snapshots failed")
        return _json({"error": str(e)}, 500)


# 12g. POST /api/generate/{job_id}/snapshot/{snapshot_id}/restore
@app.route(route="generate/{job_id}/snapshot/{snapshot_id}/restore", methods=["POST"])
def generate_restore_snapshot(req: func.HttpRequest) -> func.HttpResponse:
    job_id      = req.route_params.get("job_id")
    snapshot_id = req.route_params.get("snapshot_id")
    try:
        from generation.generation_service import restore_snapshot
        result = restore_snapshot(job_id, snapshot_id)
        return _json(result)
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("restore_snapshot failed")
        return _json({"error": str(e)}, 500)


# 12b. GET /api/generate/{job_id}/export
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/export", methods=["GET"])
def generate_export(req: func.HttpRequest) -> func.HttpResponse:
    """
    Assemble all accepted/current section versions into a final document and return it.
    Query param: format=docx|pdf|md  (overrides the format stored on the job)

    Local mode:  streams the file as a download.
    Cloud mode:  uploads to Azure Blob and returns { blob_url, filename, mime_type }.
    """
    job_id = req.route_params.get("job_id")
    fmt    = req.params.get("format")

    _fmt_map = {"docx": "Word (.docx)", "pdf": "PDF", "md": "Markdown", "markdown": "Markdown"}
    output_format = _fmt_map.get((fmt or "").lower()) if fmt else None

    try:
        from generation.doc_writer import export_job, upload_output_to_blob
        file_path, mime_type = export_job(job_id, output_format)

        # Cloud mode: upload to Azure Blob and return the URL.
        blob_url = upload_output_to_blob(job_id, file_path, mime_type)
        if blob_url:
            return _json({"job_id": job_id, "blob_url": blob_url,
                          "filename": file_path.name, "mime_type": mime_type})

        # Local / dev mode: stream the file directly.
        file_bytes = file_path.read_bytes()
        return func.HttpResponse(
            body        = file_bytes,
            status_code = 200,
            mimetype    = mime_type,
            headers     = {
                "Content-Disposition":          f'attachment; filename="{file_path.name}"',
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
            },
        )

    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_export failed")
        return _json({"error": f"Export failed: {e}"}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# TEMPLATE ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.route(route="templates", methods=["GET"])
def get_templates(req: func.HttpRequest) -> func.HttpResponse:
    """List all available templates. Query param: document_type (optional filter)."""
    doc_type = req.params.get("document_type")
    try:
        from generation.template_manager import list_templates, ensure_seeded
        ensure_seeded()
        return _json({"templates": list_templates(doc_type)})
    except Exception as e:
        logger.exception("get_templates failed")
        return _json({"error": "Failed to list templates."}, 500)


@app.route(route="templates/{template_id}/reseed", methods=["POST"])
def reseed_template_route(req: func.HttpRequest) -> func.HttpResponse:
    """Force re-seed a system template from its JSON file (use after editing a template JSON)."""
    template_id = req.route_params.get("template_id", "")
    try:
        from generation.template_manager import reseed_template
        ok = reseed_template(template_id)
        if not ok:
            return _json({"error": f"Template JSON not found: {template_id}.json"}, 404)
        return _json({"status": "reseeded", "template_id": template_id})
    except Exception as e:
        logger.exception("reseed_template failed")
        return _json({"error": str(e)}, 500)


@app.route(route="templates", methods=["POST"])
def create_template(req: func.HttpRequest) -> func.HttpResponse:
    """
    Create a user-defined template.
    Body: { name, document_type, sections: [...], system_instructions?, description? }
    """
    try:
        body = req.get_json()
        name          = body.get("name", "").strip()
        document_type = body.get("document_type", "").strip()
        sections      = body.get("sections") or []

        if not name or not document_type:
            return _json({"error": "name and document_type are required"}, 400)
        if not sections:
            return _json({"error": "sections array is required and must not be empty"}, 400)

        from generation.template_manager import save_user_template
        tmpl = save_user_template(
            name                = name,
            document_type       = document_type,
            sections            = sections,
            system_instructions = body.get("system_instructions"),
            description         = body.get("description"),
        )
        return _json({"template": tmpl.to_dict(), "message": "Template saved."}, 201)

    except Exception as e:
        logger.exception("create_template failed")
        return _json({"error": "Failed to save template."}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT ENDPOINTS  (supports the Create New Project UI)
# ═════════════════════════════════════════════════════════════════════════════

# ── DB helpers ───────────────────────────────────────────────────────────────

def _get_project_or_404(session, project_id: str):
    """Return a Project ORM object or raise FileNotFoundError."""
    from generation.db import Project as _Project
    proj = session.get(_Project, project_id)
    if proj is None:
        raise FileNotFoundError(f"Project '{project_id}' not found")
    return proj


def _apply_project_fields(proj, body: dict) -> None:
    """
    Write form-field values from *body* onto the ORM *proj* object.
    Only touches keys that are actually present in the dict — safe for PATCH.
    Stakeholders and document_ids are serialised to JSON strings.
    """
    import json as _j
    scalar_fields = [
        "project_code", "project_name", "business_unit", "business_priority",
        "problem_statement", "project_objective", "as_is_processes",
        "proposed_solution", "technical_landscape", "constraints", "risks",
        "estimated_cost_crores", "start_date", "end_date",
        "document_type", "output_format", "additional_instructions",
        "template_id", "status",
    ]
    for field in scalar_fields:
        if field in body:
            setattr(proj, field, body[field])

    if "stakeholders" in body:
        val = body["stakeholders"]
        proj.stakeholders_json = _j.dumps(val) if isinstance(val, list) else val

    if "document_ids" in body:
        val = body["document_ids"]
        proj.document_ids_json = _j.dumps(val) if isinstance(val, list) else val


def _project_code_conflict(session, code: str, exclude_id: str = None):
    """
    Return a 409 _json() response if project_code is already taken by another project.
    Returns None if the code is available (caller may proceed with the DB write).
    Pass exclude_id when updating an existing project so it may keep its own code.
    """
    if not code or not str(code).strip():
        return None
    from generation.db import Project as _Project
    q = session.query(_Project).filter(_Project.project_code == str(code).strip())
    if exclude_id:
        q = q.filter(_Project.project_id != exclude_id)
    existing = q.first()
    if existing:
        name = existing.project_name or existing.project_id
        return _json({
            "error": f"Project code '{code}' is already in use by project '{name}'. "
                     "Please choose a different project code.",
            "conflict_project_id": existing.project_id,
        }, 409)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 15. OPTIONS preflight handler (CORS for browser fetch)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="{*route}", methods=["OPTIONS"])
def cors_preflight(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        status_code = 204,
        headers     = {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 16. POST /api/extract-project-data
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="extract-project-data", methods=["POST"])
def extract_project_data(req: func.HttpRequest) -> func.HttpResponse:
    """
    Use LLM to extract project form fields from one or more parsed documents.

    Body: { "document_ids": ["uuid1", "uuid2", ...] }

    Returns a dict matching ProjectFormData fields — null for anything not found.
    The frontend uses this to auto-populate the Create New Project form.
    """
    try:
        body         = req.get_json() or {}
        document_ids = body.get("document_ids") or []

        if not document_ids:
            return _json({"error": "document_ids array is required"}, 400)

        from api.extractor import extract_project_data as _extract
        extracted = _extract(document_ids)

        return _json({
            "extracted":      extracted,
            "document_count": len(document_ids),
            "message":        (
                f"Extracted project data from {len(document_ids)} document(s). "
                "Review the fields below and fill in anything that could not be determined."
            ),
        })

    except Exception as e:
        logger.exception("extract-project-data failed")
        return _json({"error": f"Extraction failed: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 17. POST /api/projects  — create a new project (Step 1 + Step 2 form data)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects", methods=["POST"])
def create_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Persist a new project to the database.

    Body (flat JSON) — all ProjectFormData fields:
      project_name*, project_code, business_unit, business_priority,
      problem_statement*, project_objective*, as_is_processes*, proposed_solution*,
      technical_landscape*, constraints, risks, estimated_cost_crores,
      stakeholders ([{name, designation}]), start_date, end_date,
      document_type, output_format, additional_instructions,
      document_ids ([uuid]), template_id

    Returns ONLY: { project_id, status, message }
    — The frontend must call GET /api/projects/{id}/data to read back the
      saved values.  Nothing from the request body is echoed in this response.
    """
    try:
        body = req.get_json() or {}

        from generation.db import Project as _Project, DerivedData as _DerivedData, get_session
        from models.project_schema import ProjectFormData
        from uuid import uuid4

        # Validate via Pydantic (raises ValidationError on bad input)
        form = ProjectFormData(**body)

        project_id = str(uuid4())
        proj = _Project(project_id=project_id)
        proj.status = "ready" if form.project_name.strip() else "draft"

        # Populate every ingested field from the validated form
        _apply_project_fields(proj, form.model_dump())

        with get_session() as session:
            if form.project_code:
                conflict = _project_code_conflict(session, form.project_code)
                if conflict:
                    return conflict
            session.add(proj)
            # Create an empty DerivedData placeholder so the row exists immediately
            session.add(_DerivedData(project_id=project_id))
            session.commit()

        logger.info("Project created: %s ('%s')", project_id, form.project_name)
        return _json({
            "project_id": project_id,
            "status":     proj.status,
            "message":    f"Project '{form.project_name}' saved. "
                          f"Fetch GET /api/projects/{project_id}/data to read back all fields.",
        }, 201)

    except Exception as e:
        logger.exception("create_project failed")
        return _json({"error": f"Failed to save project: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 17b. POST /api/projects/draft  — create a draft project (no field validation)
# Accepts optional client-supplied "project_id" UUID in the body for idempotency.
# If the UUID already exists, returns 200 with the existing project (no duplicate created).
# This solves autosave race conditions: frontend can generate a UUID before the first
# API call and reuse it safely.
# ─────────────────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)

@app.route(route="projects/draft", methods=["POST"])
def create_draft_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Persist a draft project without requiring all mandatory fields.

    Body: any subset of project fields (all optional).
      project_id (optional): client-supplied UUID for idempotent creation.
    Returns: { project_id, status: "draft", message }
    """
    try:
        body = req.get_json() or {}
        from generation.db import Project as _Project, DerivedData as _DerivedData, get_session

        client_pid = (body.get("project_id") or "").strip()
        if client_pid:
            if not _UUID_RE.match(client_pid):
                return _json({"error": "project_id must be a valid UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)"}, 400)
            with get_session() as session:
                existing = session.get(_Project, client_pid)
                if existing:
                    return _json({"project_id": client_pid, "status": existing.status,
                                  "message": "Project already exists."}, 200)
            project_id = client_pid
        else:
            project_id = str(uuid4())

        proj = _Project(project_id=project_id)
        proj.status = "draft"
        _apply_project_fields(proj, body)

        with get_session() as session:
            if body.get("project_code"):
                conflict = _project_code_conflict(session, body["project_code"])
                if conflict:
                    return conflict
            session.add(proj)
            session.add(_DerivedData(project_id=project_id))
            session.commit()

        return _json({"project_id": project_id, "status": "draft",
                      "message": "Draft project created."}, 201)
    except Exception as e:
        logger.exception("create_draft_project failed")
        return _json({"error": f"Failed to create draft project: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 18. GET /api/projects  — list all projects (dashboard table)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects", methods=["GET"])
def list_projects(req: func.HttpRequest) -> func.HttpResponse:
    """
    Return all saved projects as lightweight summaries (most recent first).

    Query params:
      q         — fuzzy search by project_name or project_code (case-insensitive)
      code      — exact project_code lookup (use when you have code but need UUID)
      status    — filter by status  (draft|ready|generating|completed)
      page      — page number (default 1)
      per_page  — items per page (default 50, max 100)
    """
    try:
        from generation.db import Project as _Project, get_session
        from sqlalchemy import or_, func as sqlfunc

        q        = (req.params.get("q")        or "").strip().lower()
        status   = (req.params.get("status")   or "").strip()
        code     = (req.params.get("code")     or "").strip()
        page     = int(req.params.get("page",  1) or 1)
        per_page = int(req.params.get("per_page", 50) or 50)

        # Clamp to safe ranges
        page = max(1, page)
        per_page = min(100, max(1, per_page))

        with get_session() as session:
            query = session.query(_Project)
            if code:
                # Exact project_code lookup — resolves code → UUID
                query = query.filter(_Project.project_code == code)
            elif q:
                query = query.filter(
                    or_(
                        sqlfunc.lower(_Project.project_name).contains(q),
                        sqlfunc.lower(_Project.project_code).contains(q),
                    )
                )
            if status:
                query = query.filter(_Project.status == status)

            total = query.count()
            projects = query.order_by(_Project.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
            summaries = [p.to_summary_dict() for p in projects]
            pages = (total + per_page - 1) // per_page

        return _json({
            "projects": summaries,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "count": len(summaries),
        })

    except Exception as e:
        logger.exception("list_projects failed")
        return _json({"error": "Failed to list projects."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19. GET /api/projects/{project_id}  — get single project (full ingested fields)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}", methods=["GET"])
def get_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Return the full saved project (all ingested fields + lifecycle metadata).
    Does NOT include derived data — use GET /api/projects/{id}/data for that.
    """
    project_id = req.route_params.get("project_id")
    try:
        from generation.db import get_session
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            return _json(proj.to_full_dict())
    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("get_project failed")
        return _json({"error": "Failed to retrieve project."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19b. DELETE /api/projects/{project_id}  — hard delete (cascade to derived_data)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}", methods=["DELETE"])
def delete_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Permanently delete a project and its derived data row (cascade).
    Used by the Dashboard trash icon.

    Returns: 204 No Content on success.
    """
    project_id = req.route_params.get("project_id")
    try:
        from generation.db import get_session

        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            session.delete(proj)   # cascade="all, delete-orphan" removes DerivedData row too
            session.commit()

        return func.HttpResponse(
            status_code = 204,
            headers     = {
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
            },
        )

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("delete_project failed")
        return _json({"error": f"Failed to delete project: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19c. PUT /api/projects/{project_id}  [DEPRECATED]
# Use PATCH /api/projects/{project_id} instead — identical behaviour.
# PUT is kept for backwards compatibility but will be removed in a future release.
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}", methods=["PUT"])
def update_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    DEPRECATED — use PATCH /api/projects/{project_id}.
    Body: any subset of project fields.
    Returns: { project_id, updated_at, deprecated }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json() or {}
        from generation.db import get_session
        from datetime import datetime as _dt

        now = _dt.utcnow()
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            if body.get("project_code"):
                conflict = _project_code_conflict(session, body["project_code"], exclude_id=project_id)
                if conflict:
                    return conflict
            _apply_project_fields(proj, body)
            proj.updated_at = now
            session.commit()

        resp = _json({"project_id": project_id, "updated_at": now.isoformat(),
                      "deprecated": "Use PATCH /api/projects/{id} instead of PUT."})
        resp.headers["X-Deprecated"] = "Use PATCH /api/projects/{id} — PUT will be removed in a future release"
        return resp

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("update_project failed")
        return _json({"error": f"Failed to update project: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19d. PATCH /api/projects/{project_id}  — partial update / autosave
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}", methods=["PATCH"])
def patch_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Partial update — only the fields present in the body are written.
    Used by the frontend's 30-second autosave while the user is still filling
    in the Create Project form.

    Body: any subset of project fields.
    Returns: { project_id, updated_at }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json() or {}
        if not body:
            return _json({"error": "Request body is empty."}, 400)

        from generation.db import get_session
        from datetime import datetime as _dt

        now = _dt.utcnow()
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            if body.get("project_code"):
                conflict = _project_code_conflict(session, body["project_code"], exclude_id=project_id)
                if conflict:
                    return conflict
            _apply_project_fields(proj, body)
            proj.updated_at = now
            session.commit()

        return _json({"project_id": project_id, "updated_at": now.isoformat()})

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("patch_project failed")
        return _json({"error": f"Failed to autosave project: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19e. GET /api/projects/{project_id}/data  — Project Data page
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}/data", methods=["GET"])
def get_project_data(req: func.HttpRequest) -> func.HttpResponse:
    """
    Return both ingested and derived data for the Project Data sub-page.

    Response:
    {
      "project_id": "...",
      "ingested": {          ← 15 user-entered fields
        "project_name": "...",
        "project_code": "...",
        "business_unit": "...",
        "business_priority": "...",
        "problem_statement": "...",
        "project_objective": "...",
        "stakeholders": [{name, designation}],
        "start_date": "...",
        "end_date": "...",
        "as_is_processes": "...",
        "proposed_solution": "...",
        "constraints": "...",
        "risks": "...",
        "technical_landscape": "...",
        "estimated_cost_crores": "..."
      },
      "derived": {           ← 12 AI-generated fields (empty strings if not yet run)
        "current_challenges": "...",
        "to_be_process": "...",
        "success_criteria": "...",
        "business_requirements": "...",
        "functional_requirements": "...",
        "non_functional_requirements": "...",
        "industry_benchmarks": "...",
        "workflow": "...",
        "analytics_requirements": "...",
        "systems_involved": "...",
        "data_sources": "...",
        "constraints_dependencies": "..."
      },
      "derived_generated_at": "ISO timestamp or null"
    }
    """
    project_id = req.route_params.get("project_id")
    try:
        from generation.db import DerivedData as _DerivedData, get_session

        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            ingested = proj.to_ingested_dict()

            # Remove non-ingested lifecycle keys from the ingested block
            for k in ("project_id", "status", "job_id", "created_at", "updated_at",
                       "document_type", "output_format", "additional_instructions",
                       "document_ids", "template_id"):
                ingested.pop(k, None)

            derived_row = session.get(_DerivedData, project_id)
            if derived_row:
                derived      = derived_row.to_dict()
                generated_at = derived.pop("generated_at", None)
                derived.pop("project_id", None)
                derived.pop("updated_at", None)
            else:
                derived = {
                    "current_challenges": "", "to_be_process": "",
                    "success_criteria": "", "business_requirements": "",
                    "functional_requirements": "", "non_functional_requirements": "",
                    "industry_benchmarks": "", "workflow": "",
                    "analytics_requirements": "", "systems_involved": "",
                    "data_sources": "", "constraints_dependencies": "",
                }
                generated_at = None

        return _json({
            "project_id":          project_id,
            "ingested":            ingested,
            "derived":             derived,
            "derived_generated_at": generated_at,
        })

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("get_project_data failed")
        return _json({"error": "Failed to retrieve project data."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19f. PUT /api/projects/{project_id}/data/ingested  — save ingested field edits
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}/data/ingested", methods=["PUT"])
def update_ingested_data(req: func.HttpRequest) -> func.HttpResponse:
    """
    Save user edits to the ingested (user-entered) project fields.
    Called when the user clicks "Save" on the Ingested Details tab of the
    Project Data sub-page.

    Body: any subset of the 15 ingested fields (partial update is safe).
    Writable fields:
      project_name, project_code, business_unit, business_priority,
      problem_statement, project_objective, stakeholders, start_date, end_date,
      as_is_processes, proposed_solution, constraints, risks,
      technical_landscape, estimated_cost_crores

    Returns: { project_id, updated_at }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json() or {}
        if not body:
            return _json({"error": "Request body is empty."}, 400)

        # Guard: reject fields that live outside ingested scope
        non_ingested = {"status", "job_id", "document_type", "output_format",
                        "additional_instructions", "document_ids", "template_id"}
        body = {k: v for k, v in body.items() if k not in non_ingested}

        from generation.db import get_session
        from datetime import datetime as _dt

        now = _dt.utcnow()
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            if body.get("project_code"):
                conflict = _project_code_conflict(session, body["project_code"], exclude_id=project_id)
                if conflict:
                    return conflict
            _apply_project_fields(proj, body)
            proj.updated_at = now
            session.commit()

        return _json({"project_id": project_id, "updated_at": now.isoformat()})

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("update_ingested_data failed")
        return _json({"error": f"Failed to save ingested data: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19g. PUT /api/projects/{project_id}/data/derived  — save derived field edits
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}/data/derived", methods=["PUT"])
def update_derived_data(req: func.HttpRequest) -> func.HttpResponse:
    """
    Save user edits to the AI-derived fields (Derived Details tab on Project
    Data sub-page).  Also used internally when the AI populates them.

    Body: any subset of the 12 derived fields:
      current_challenges, to_be_process, success_criteria,
      business_requirements, functional_requirements,
      non_functional_requirements, industry_benchmarks, workflow,
      analytics_requirements, systems_involved, data_sources,
      constraints_dependencies

    Optional body field:
      mark_as_generated (bool) — if true, sets generated_at = now()
                                  (used when AI populates all 12 fields at once)

    Returns: { project_id, updated_at }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json() or {}
        if not body:
            return _json({"error": "Request body is empty."}, 400)

        from generation.db import DerivedData as _DerivedData, get_session
        from datetime import datetime as _dt

        derived_fields = [
            "current_challenges", "to_be_process", "success_criteria",
            "business_requirements", "functional_requirements",
            "non_functional_requirements", "industry_benchmarks", "workflow",
            "analytics_requirements", "systems_involved", "data_sources",
            "constraints_dependencies",
        ]
        mark_generated = bool(body.get("mark_as_generated", False))

        with get_session() as session:
            # Ensure the parent project exists
            _get_project_or_404(session, project_id)

            # Upsert derived row
            row = session.get(_DerivedData, project_id)
            if row is None:
                row = _DerivedData(project_id=project_id)
                session.add(row)

            for field in derived_fields:
                if field in body:
                    setattr(row, field, body[field])

            now = _dt.utcnow()
            row.updated_at = now
            if mark_generated:
                row.generated_at = now

            session.commit()

        return _json({"project_id": project_id, "updated_at": now.isoformat()})

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("update_derived_data failed")
        return _json({"error": f"Failed to save derived data: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19h. POST /api/projects/{project_id}/derive-fields  — AI-derive the 12 extended fields
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}/derive-fields", methods=["POST"])
def derive_project_fields(req: func.HttpRequest) -> func.HttpResponse:
    """
    Use LLM to derive the 12 extended DerivedData fields from the project's
    saved ingested data.  Reads everything from DB — no body required.

    This is the "AI Analyze" / "Auto-Derive" button on the Project Data page.

    Flow:
      1. Load all 15 ingested fields from Project table
      2. Load source document content (if document_ids present) for richer context
      3. Call LLM → derive 12 structured fields (current_challenges,
         to_be_process, success_criteria, business_requirements,
         functional_requirements, non_functional_requirements,
         industry_benchmarks, workflow, analytics_requirements,
         systems_involved, data_sources, constraints_dependencies)
      4. Upsert DerivedData row, set generated_at = now()
      5. Return { project_id, status, message, fields_populated }

    Returns: { project_id, status, message, fields_populated, updated_at }
    """
    project_id = req.route_params.get("project_id")
    try:
        from generation.db import DerivedData as _DerivedData, get_session
        from generation.derive_fields import derive_project_fields as _derive
        from datetime import datetime as _dt

        # ── 1. Load project from DB ───────────────────────────────────────────
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            project_data = proj.to_full_dict()
            doc_ids      = proj.document_ids   # list of uploaded document UUIDs

        logger.info(
            "derive-fields: starting for project '%s' (%d docs attached)",
            project_data.get("project_name", project_id), len(doc_ids),
        )

        # ── 2. Call LLM — this may take 30–90 seconds ────────────────────────
        derived = _derive(project_data, doc_ids)

        # ── 3. Persist derived fields to DB ───────────────────────────────────
        now = _dt.utcnow()
        with get_session() as session:
            row = session.get(_DerivedData, project_id)
            if row is None:
                row = _DerivedData(project_id=project_id)
                session.add(row)

            for field, value in derived.items():
                if value:   # only overwrite if LLM returned something
                    setattr(row, field, value)

            row.generated_at = now
            row.updated_at   = now
            session.commit()

        fields_populated = sum(1 for v in derived.values() if v)
        logger.info(
            "derive-fields: saved %d/12 fields for project '%s'",
            fields_populated, project_id,
        )

        return _json({
            "project_id":       project_id,
            "status":           "derived",
            "fields_populated": fields_populated,
            "updated_at":       now.isoformat(),
            "message": (
                f"AI analysis complete. {fields_populated}/12 fields derived from project context. "
                f"Fetch GET /api/projects/{project_id}/data to read the derived fields."
            ),
        })

    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except RuntimeError as e:
        # LLM call failed — return 502 so frontend can show a retry button
        logger.error("derive-fields failed for %s: %s", project_id, e)
        return _json({"error": f"AI derivation failed: {e}"}, 502)
    except Exception as e:
        logger.exception("derive-fields unexpected error")
        return _json({"error": f"Failed to derive fields: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 19i. POST /api/projects/{project_id}/validate  — pre-flight check before Generate
# Checks required fields are filled without starting generation.
# Frontend calls this to decide whether to enable the Generate button.
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}/validate", methods=["POST"])
def validate_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns: { project_id, valid, ready_to_generate, missing_required[], message }
    """
    project_id = req.route_params.get("project_id")
    try:
        from generation.db import get_session
        REQUIRED = [
            "project_name", "problem_statement", "project_objective",
            "as_is_processes", "proposed_solution", "technical_landscape",
        ]
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            data = proj.to_ingested_dict()
        missing = [f for f in REQUIRED if not (data.get(f) or "").strip()]
        ready   = len(missing) == 0
        return _json({
            "project_id":        project_id,
            "valid":             ready,
            "ready_to_generate": ready,
            "missing_required":  missing,
            "message": "Ready to generate." if ready else
                       f"{len(missing)} required field(s) missing: {', '.join(missing)}",
        })
    except FileNotFoundError:
        return _json({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        logger.exception("validate_project failed")
        return _json({"error": f"Validation check failed: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# 20. POST /api/generate/project/{project_id}  — start generation from project
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/project/{project_id}", methods=["POST"])
def generate_from_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Start a generation job from a saved project.
    Reads all project fields from DB — no body required.

    Returns: { job_id, status, sections[], message }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json()
    except Exception:
        body = {}
    try:
        from generation.generation_service import start_job_from_project
        job = start_job_from_project(
            project_id,
            allow_no_docs=True,
            doc_type_override=body.get("document_type") if body else None,
        )
        return _json({
            "job_id":  job["job_id"],
            "status":  job["status"],
            "sections": [
                {
                    "section_id":    s["section_id"],
                    "section_title": s["section_title"],
                    "status":        s["status"],
                }
                for s in job.get("sections", [])
            ],
            "message": f"Generation started. {job['total_sections']} sections queued.",
        }, 201)
    except FileNotFoundError as e:
        return _json({"error": str(e)}, 404)
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate_from_project failed")
        return _json({"error": f"Failed to start generation: {e}"}, 500)



# ═════════════════════════════════════════════════════════════════════════════
# CHAT STUDIO ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# POST /api/chat/init  — create session + opening assistant message
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="chat/init", methods=["POST"])
def chat_init(req: func.HttpRequest) -> func.HttpResponse:
    """
    Create a new chat session for a project + document type.
    Returns the session_id and an opening assistant message.

    Body: { project_id?, document_type?, project_name? }

    project_id is optional. If omitted, a new draft project is auto-created
    so the user can start a chat-first workflow without creating a project first.
    The response includes auto_created_project_id in that case.
    """
    try:
        body         = req.get_json() or {}
        project_id   = (body.get("project_id") or "").strip()
        doc_type     = (body.get("document_type") or "brd").strip()
        project_name = (body.get("project_name") or "").strip()
        auto_created = False

        if not project_id:
            from generation.db import Project as _Project, DerivedData as _DerivedData, get_session
            project_id = str(uuid4())
            proj = _Project(project_id=project_id, status="draft")
            if project_name:
                proj.project_name = project_name
            if doc_type:
                proj.document_type = doc_type
            with get_session() as session:
                session.add(proj)
                session.add(_DerivedData(project_id=project_id))
                session.commit()
            auto_created = True
            logger.info("chat/init: auto-created draft project %s", project_id)

        from api.chat_handler import init_session
        result = init_session(project_id, doc_type, project_name)
        if auto_created:
            result["auto_created_project_id"] = project_id
            result["note"] = "A new draft project was created for this chat session. Use auto_created_project_id for subsequent project API calls."
        return _json(result, 201)

    except Exception as e:
        logger.exception("chat/init failed")
        return _json({"error": f"Failed to init chat: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/chat/message  — process a user message
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="chat/message", methods=["POST"])
def chat_message(req: func.HttpRequest) -> func.HttpResponse:
    """
    Send a user message and receive an assistant response.
    The handler classifies intent and routes to the right action.

    Body: { session_id, message, project_id?, document_type? }
    """
    try:
        body       = req.get_json() or {}
        session_id = body.get("session_id", "").strip()
        message    = body.get("message", "").strip()
        project_id = body.get("project_id")
        doc_type   = body.get("document_type")

        if not session_id:
            return _json({"error": "session_id is required"}, 400)
        if not message:
            return _json({"error": "message is required"}, 400)

        from api.chat_handler import process_message
        result = process_message(session_id, message, project_id, doc_type)
        return _json(result)

    except Exception as e:
        logger.exception("chat/message failed")
        return _json({"error": f"Failed to process message: {e}"}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/chat/{session_id}/history  — fetch full message history
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="chat/{session_id}/history", methods=["GET"])
def chat_history(req: func.HttpRequest) -> func.HttpResponse:
    """Return the full ChatSession record including all messages."""
    session_id = req.route_params.get("session_id")
    try:
        from api.chat_handler import get_history
        return _json(get_history(session_id))
    except ValueError as e:
        return _json({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("chat_history failed")
        return _json({"error": "Failed to retrieve chat history."}, 500)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/chat/{session_id}/upload  — upload a document via chat
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="chat/{session_id}/upload", methods=["POST"])
def chat_upload(req: func.HttpRequest) -> func.HttpResponse:
    """
    Parse and store a file uploaded from the chat UI.
    Attaches the resulting document_id to the session's project.
    Body: multipart/form-data with 'file' field.
    """
    import tempfile
    from pathlib import Path as _Path

    session_id = req.route_params.get("session_id")
    try:
        from parsers.parser_factory import parse_document, SUPPORTED_EXTENSIONS
        from storage.azure_storage import get_storage_service
        from api.chat_handler import attach_document_to_session

        file_data = req.files.get("file")
        if not file_data:
            return _json({"error": "No file provided. Send as multipart field 'file'."}, 400)

        filename = getattr(file_data, "filename", None) or "upload"
        ext = _Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            return _json({"error": f"Unsupported file type '{ext}'.",
                          "supported": SUPPORTED_EXTENSIONS}, 415)

        raw = file_data.read() if hasattr(file_data, "read") else file_data
        if len(raw) > 50 * 1024 * 1024:
            return _json({"error": "File too large. Max 50 MB."}, 413)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(raw)
            tmp_path = _Path(tmp.name)

        try:
            parsed_doc = parse_document(tmp_path)
            parsed_doc.source_filename = filename
            store      = get_storage_service()
            parsed_doc = store.persist_all(parsed_doc, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        result = attach_document_to_session(session_id, parsed_doc.document_id, filename)
        return _json(result, 201)

    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("chat_upload failed")
        return _json({"error": f"Upload failed: {e}"}, 500)
