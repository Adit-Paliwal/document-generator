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
# 6. POST /api/generate/start
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/start", methods=["POST"])
def generate_start(req: func.HttpRequest) -> func.HttpResponse:
    """
    Create a generation job and start background section generation.
    Returns immediately with the job_id — client polls /api/generate/{job_id}.

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

        return _json({
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
        }, 201)

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
# 12. GET /api/generate/{job_id}/export
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="generate/{job_id}/export", methods=["GET"])
def generate_export(req: func.HttpRequest) -> func.HttpResponse:
    """
    Assemble all accepted/current section versions into a final document and return it.
    Query param: format=docx|pdf|md  (overrides the format stored on the job)
    """
    job_id = req.route_params.get("job_id")
    fmt    = req.params.get("format")  # optional override

    # Map short format param → full format string
    _fmt_map = {"docx": "Word (.docx)", "pdf": "PDF", "md": "Markdown", "markdown": "Markdown"}
    output_format = _fmt_map.get((fmt or "").lower()) if fmt else None

    try:
        from generation.doc_writer import export_job
        file_path, mime_type = export_job(job_id, output_format)

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
# 18. GET /api/projects  — list all projects (dashboard table)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects", methods=["GET"])
def list_projects(req: func.HttpRequest) -> func.HttpResponse:
    """
    Return all saved projects as lightweight summaries (most recent first).

    Optional query params:
      q         — search by project_name or project_code (case-insensitive)
      status    — filter by status  (draft|ready|generating|completed)
    """
    try:
        from generation.db import Project as _Project, get_session
        from sqlalchemy import or_, func as sqlfunc

        q      = (req.params.get("q")      or "").strip().lower()
        status = (req.params.get("status") or "").strip()

        with get_session() as session:
            query = session.query(_Project)
            if q:
                query = query.filter(
                    or_(
                        sqlfunc.lower(_Project.project_name).contains(q),
                        sqlfunc.lower(_Project.project_code).contains(q),
                    )
                )
            if status:
                query = query.filter(_Project.status == status)
            projects = query.order_by(_Project.created_at.desc()).all()
            summaries = [p.to_summary_dict() for p in projects]

        return _json({"projects": summaries, "count": len(summaries)})

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
# 19c. PUT /api/projects/{project_id}  — full update (form re-submit)
# ─────────────────────────────────────────────────────────────────────────────

@app.route(route="projects/{project_id}", methods=["PUT"])
def update_project(req: func.HttpRequest) -> func.HttpResponse:
    """
    Full replace-update of all project form fields.
    Used when the user explicitly edits and saves the project form.

    Body: same flat JSON as POST /api/projects (all fields optional for update).
    Returns: { project_id, updated_at }
    """
    project_id = req.route_params.get("project_id")
    try:
        body = req.get_json() or {}
        from generation.db import get_session
        from datetime import datetime as _dt

        now = _dt.utcnow()
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            _apply_project_fields(proj, body)
            proj.updated_at = now
            session.commit()

        return _json({"project_id": project_id, "updated_at": now.isoformat()})

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
        from generation.db import get_session
        from datetime import datetime as _dt

        with get_session() as session:
            proj = _get_project_or_404(session, project_id)

            doc_ids = proj.document_ids
            if not doc_ids:
                return _json({"error": "No documents attached to this project."}, 400)

            # Build a clean dict of all ingested fields for generation
            fd = proj.to_full_dict()

        primary_doc_id = doc_ids[0]

        # Build stakeholders string
        stakeholders_str = ", ".join(
            f"{s.get('name', '')} ({s.get('designation', '')})"
            for s in (fd.get("stakeholders") or [])
            if s.get("name")
        ) or None

        # Assemble extra context into additional_instructions
        extra_parts = []
        for label, key in [
            ("Constraints",        "constraints"),
            ("Risks & Mitigation", "risks"),
            ("Technical Landscape","technical_landscape"),
            ("Business Priority",  "business_priority"),
            ("Project Code",       "project_code"),
            ("Business Unit",      "business_unit"),
        ]:
            if fd.get(key):
                extra_parts.append(f"{label}: {fd[key]}")
        if fd.get("start_date") or fd.get("end_date"):
            extra_parts.append(
                f"Timeline: {fd.get('start_date', 'TBD')} to {fd.get('end_date', 'TBD')}"
            )
        if fd.get("estimated_cost_crores"):
            extra_parts.append(f"Estimated Cost: ₹{fd['estimated_cost_crores']} Crores")
        if fd.get("additional_instructions"):
            extra_parts.append(fd["additional_instructions"])

        user_inputs = {
            "project_name":            fd.get("project_name", ""),
            "document_type":           fd.get("document_type", "BRD"),
            "output_format":           fd.get("output_format", "Word (.docx)"),
            "stakeholders":            stakeholders_str,
            "project_description":     fd.get("proposed_solution") or fd.get("project_objective", ""),
            "business_problem":        fd.get("problem_statement"),
            "expected_outcome":        fd.get("project_objective"),
            "additional_instructions": "\n\n".join(extra_parts) if extra_parts else None,
        }

        from generation.generation_service import start_job
        job = start_job(primary_doc_id, user_inputs, fd.get("template_id"))

        # Persist job_id + status back to DB
        with get_session() as session:
            proj = _get_project_or_404(session, project_id)
            proj.job_id     = job["job_id"]
            proj.status     = "generating"
            proj.updated_at = _dt.utcnow()
            session.commit()

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
            "message": (
                f"Generation started for '{fd.get('project_name', project_id)}'. "
                f"{job['total_sections']} sections queued. "
                f"Poll GET /api/generate/{job['job_id']} for progress."
            ),
        }, 201)

    except FileNotFoundError as e:
        return _json({"error": str(e)}, 404)
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate_from_project failed")
        return _json({"error": f"Failed to start generation: {e}"}, 500)
