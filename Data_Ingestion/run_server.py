"""
Intellidraft — Standalone Flask API Server
==========================================
Runs all API endpoints on http://localhost:7071/api
without needing Azure Functions Core Tools.

HOW TO RUN (from the Intellidraft/ directory):
    env\\Scripts\\python.exe Data_Ingestion\\run_server.py

Then open  frontend/index.html  in your browser
(or run:  python frontend/serve.py  and go to http://localhost:3000)
"""

from __future__ import annotations
import atexit
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# ── Venv / dependency guard ───────────────────────────────────────────────────
# Catch the most common mistake: running with system Python instead of the venv.
# We check for python-docx (the most distinctive package) early so the user gets
# a clear error at startup instead of a cryptic 500 on the first upload.
_REQUIRED = {
    "docx":   "python-docx",
    "flask":  "flask",
    "dotenv": "python-dotenv",
}
_missing = []
for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod)
    except ModuleNotFoundError:
        _missing.append(_pkg)

if _missing:
    print("\n" + "="*62)
    print("  ERROR — Missing packages. Wrong Python interpreter?")
    print("  Running with:", sys.executable)
    print("  Missing:     ", ", ".join(_missing))
    print()
    print("  Fix — run with the virtual environment:")
    print("    env\\Scripts\\Activate.ps1  (PowerShell)")
    print("    python Data_Ingestion\\run_server.py")
    print()
    print("  Or directly:")
    print("    env\\Scripts\\python.exe Data_Ingestion\\run_server.py")
    print("="*62 + "\n")
    sys.exit(1)

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
_BASE = Path(__file__).parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# ── Load .env ─────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(dotenv_path=_BASE / ".env", override=False)

# ── Flask ─────────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)

# ── Logging ────────────────────────────────────────────────────────────────────
# basicConfig sets the root handler; the werkzeug logger inherits it.
# We also force werkzeug to stdout so access lines appear in all terminals.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout,
)
# Ensure werkzeug (Flask's HTTP layer) sends its lines to stdout too
_wz = logging.getLogger("werkzeug")
_wz.setLevel(logging.INFO)
if not _wz.handlers:
    _wz_h = logging.StreamHandler(sys.stdout)
    _wz_h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
    _wz.addHandler(_wz_h)

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB

# ── Graceful DB shutdown ───────────────────────────────────────────────────────
# SQLAlchemy StaticPool keeps the SQLite connection open until engine.dispose()
# is called. Without this, the -wal / -shm files stay locked after Ctrl+C.
def _shutdown_db():
    try:
        from generation import db as _db
        if _db._engine is not None:
            _db._engine.dispose()
            print("  [DB] connections closed — WAL flushed.")
    except Exception:
        pass   # best-effort; never break the shutdown

atexit.register(_shutdown_db)

# Lazy singletons
_store = None

def _get_store():
    global _store
    if _store is None:
        from storage.azure_storage import get_storage_service
        _store = get_storage_service()
    return _store

# ── CORS helper ───────────────────────────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, x-functions-key",
}

def json_resp(data: dict, status: int = 200) -> Response:
    r = jsonify(data)
    r.status_code = status
    for k, v in CORS_HEADERS.items():
        r.headers[k] = v
    return r

@app.after_request
def add_cors(response):
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response

@app.after_request
def log_request(response):
    """Log every request with method, path, and HTTP status code."""
    # Skip logging for static/options to keep output clean
    if request.method not in ("OPTIONS", "HEAD"):
        logger.info("%-6s %-45s → %s", request.method, request.path, response.status_code)
    return response

@app.route("/api/<path:p>", methods=["OPTIONS", "HEAD"])
def options_handler(p):
    return Response(status=204, headers=CORS_HEADERS)


# ═════════════════════════════════════════════════════════════════════════════
# INGESTION ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 0a. GET /api/health  — lightweight liveness probe (used by Docker HEALTHCHECK)
@app.route("/api/health", methods=["GET"])
def health():
    return json_resp({"status": "ok", "version": "1.0"})


# 0b. GET /api/docs  — interactive API reference
@app.route("/api/docs", methods=["GET"])
def api_docs():
    docs_path = _BASE / "api-docs.html"
    if docs_path.exists():
        return send_file(str(docs_path), mimetype="text/html")
    return json_resp({"error": "API docs not found."}, 404)


# 0c. POST /api/admin/reset-db  — DEV ONLY: wipe and recreate the SQLite database
@app.route("/api/admin/reset-db", methods=["POST"])
def admin_reset_db():
    """
    Development helper — deletes the SQLite database and recreates all tables.
    Call from Postman or the browser console when you want a clean slate.
    NOT safe in production (no auth guard).
    """
    try:
        import generation.db as _db
        from pathlib import Path as _P

        # 1. Dispose engine — flushes WAL, releases file handles
        if _db._engine is not None:
            _db._engine.dispose()
            _db._engine = None

        # 2. Delete DB + WAL/SHM files
        db_url = _db.DATABASE_URL
        deleted = []
        if db_url.startswith("sqlite:///"):
            db_path = _P(db_url.replace("sqlite:///", ""))
            for suffix in ("", "-wal", "-shm"):
                f = _P(str(db_path) + suffix) if suffix else db_path
                if f.exists():
                    f.unlink()
                    deleted.append(f.name)

        # 3. Recreate — get_engine() calls Base.metadata.create_all()
        _db.get_engine()
        logger.info("DB reset: deleted %s, tables recreated.", deleted)
        return json_resp({"status": "ok", "deleted": deleted,
                          "message": "Database wiped and recreated."})
    except Exception as e:
        logger.exception("admin_reset_db failed")
        return json_resp({"error": str(e)}, 500)


# 1. GET /api/form-fields
@app.route("/api/form-fields", methods=["GET"])
def get_form_fields():
    from api.user_input_schema import DOCUMENT_FORM_FIELDS
    return json_resp({"fields": [f.model_dump() for f in DOCUMENT_FORM_FIELDS]})


# 2. POST /api/upload
@app.route("/api/upload", methods=["POST"])
def upload_document():
    try:
        from parsers.parser_factory import parse_document, SUPPORTED_EXTENSIONS

        file_data = request.files.get("file")
        if not file_data:
            return json_resp({"error": "No file provided. Send as multipart field 'file'."}, 400)

        filename = file_data.filename or "upload"
        ext      = Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            return json_resp({"error": f"Unsupported file type '{ext}'.",
                              "supported": SUPPORTED_EXTENSIONS}, 415)

        raw = file_data.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return json_resp({"error": f"File too large. Max 50 MB."}, 413)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)

        try:
            parsed_doc = parse_document(tmp_path)
            parsed_doc.source_filename = filename
            parsed_doc = _get_store().persist_all(parsed_doc, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        return json_resp({
            "document_id": parsed_doc.document_id,
            "filename":    filename,
            "file_type":   parsed_doc.file_type,
            "blob_base":   parsed_doc.blob_base_path,
            "summary":     parsed_doc.summary.model_dump(),
            "message": (
                f"Document parsed. "
                f"{parsed_doc.summary.total_text_elements} text blocks, "
                f"{parsed_doc.summary.total_images} images, "
                f"{parsed_doc.summary.total_tables} tables."
            ),
        }, 201)

    except ValueError as e:
        return json_resp({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("upload failed")
        return json_resp({"error": f"Parsing failed: {e}"}, 500)


# 3. POST /api/submit-inputs
@app.route("/api/submit-inputs", methods=["POST"])
def submit_user_inputs():
    try:
        from api.user_input_schema import UserInputRequest
        from models.meta_schema    import UserInputData, ParsedDocument

        body     = request.get_json()
        req_data = UserInputRequest(**body)
        meta     = _get_store().get_meta_json(req_data.document_id)
        doc      = ParsedDocument(**meta)
        doc.user_inputs = UserInputData(**req_data.model_dump(exclude={"document_id"}))
        _get_store().save_meta_json(doc)
        _get_store().save_to_cosmos(doc)
        return json_resp({"document_id": req_data.document_id,
                          "message": "Inputs saved. Ready for generation."})
    except FileNotFoundError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("submit-inputs failed")
        return json_resp({"error": str(e)}, 500)


# 4. GET /api/document/<doc_id>
@app.route("/api/document/<doc_id>", methods=["GET"])
def get_document(doc_id):
    try:
        return json_resp(_get_store().get_meta_json(doc_id))
    except FileNotFoundError:
        return json_resp({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 5. GET /api/document/<doc_id>/status
@app.route("/api/document/<doc_id>/status", methods=["GET"])
def get_document_status(doc_id):
    try:
        return json_resp(_get_store().get_document_index(doc_id))
    except FileNotFoundError:
        return json_resp({"error": f"Document '{doc_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# GENERATION ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 6. POST /api/generate/start
@app.route("/api/generate/start", methods=["POST"])
def generate_start():
    try:
        from generation.generation_service import start_job
        body        = request.get_json()
        document_id = body.get("document_id")
        user_inputs = body.get("user_inputs") or {}
        template_id = body.get("template_id")
        if not document_id:
            return json_resp({"error": "document_id is required"}, 400)
        if not user_inputs.get("document_type"):
            return json_resp({"error": "user_inputs.document_type is required"}, 400)
        job = start_job(document_id, user_inputs, template_id)
        return json_resp({
            "job_id":   job["job_id"],
            "status":   job["status"],
            "sections": [{"section_id": s["section_id"],
                          "section_title": s["section_title"],
                          "status": s["status"]}
                         for s in job.get("sections", [])],
            "message": f"{job['total_sections']} sections queued.",
        }, 201)
    except ValueError as e:
        return json_resp({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate/start failed")
        return json_resp({"error": str(e)}, 500)


# 7. GET /api/generate/<job_id>
@app.route("/api/generate/<job_id>", methods=["GET"])
def generate_get_job(job_id):
    try:
        from generation.generation_service import get_job
        job = get_job(job_id)
        for sec in job.get("sections", []):
            current_v = sec.get("current_version", 0)
            versions  = sec.get("versions", [])
            current   = next((v for v in versions if v["version_number"] == current_v), None)
            sec["current_content"] = current["content"] if current else None
            sec["version_count"]   = len(versions)
            sec.pop("versions", None)
        return json_resp(job)
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("generate_get_job failed")
        return json_resp({"error": str(e)}, 500)


# 8. GET /api/generate/<job_id>/section/<section_id>
@app.route("/api/generate/<job_id>/section/<section_id>", methods=["GET"])
def generate_get_section(job_id, section_id):
    try:
        from generation.generation_service import get_section
        return json_resp(get_section(section_id))
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 9. POST /api/generate/<job_id>/section/<section_id>/comment
@app.route("/api/generate/<job_id>/section/<section_id>/comment", methods=["POST"])
def generate_add_comment(job_id, section_id):
    try:
        from generation.generation_service import add_comment
        body         = request.get_json()
        comment_text = body.get("comment_text", "").strip()
        comment_type = body.get("comment_type", "edit_request")
        if not comment_text:
            return json_resp({"error": "comment_text is required"}, 400)
        comment = add_comment(section_id, comment_text, comment_type)
        return json_resp({"comment": comment, "message": "Comment saved."}, 201)
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 10. POST /api/generate/<job_id>/section/<section_id>/regenerate
@app.route("/api/generate/<job_id>/section/<section_id>/regenerate", methods=["POST"])
def generate_regenerate(job_id, section_id):
    try:
        from generation.generation_service import regenerate_section
        body       = request.get_json() or {}
        comment_id = body.get("comment_id")
        new_version = regenerate_section(section_id, comment_id)
        return json_resp({"new_version": new_version,
                          "message": f"Regenerated — version {new_version['version_number']}."})
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 11. POST /api/generate/<job_id>/section/<section_id>/accept
@app.route("/api/generate/<job_id>/section/<section_id>/accept", methods=["POST"])
def generate_accept(job_id, section_id):
    try:
        from generation.generation_service import accept_version
        body           = request.get_json()
        version_number = body.get("version_number")
        if version_number is None:
            return json_resp({"error": "version_number is required"}, 400)
        accepted = accept_version(section_id, int(version_number))
        return json_resp({"accepted_version": accepted,
                          "message": f"Version {version_number} accepted."})
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 12. GET /api/generate/<job_id>/export
@app.route("/api/generate/<job_id>/export", methods=["GET"])
def generate_export(job_id):
    fmt = request.args.get("format", "")
    _fmt_map = {"docx": "Word (.docx)", "pdf": "PDF",
                "md": "Markdown", "markdown": "Markdown"}
    output_format = _fmt_map.get(fmt.lower()) if fmt else None
    try:
        from generation.doc_writer import export_job
        file_path, mime_type = export_job(job_id, output_format)
        return send_file(
            str(file_path),
            mimetype       = mime_type,
            as_attachment  = True,
            download_name  = file_path.name,
        )
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("export failed")
        return json_resp({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# TEMPLATE ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

# 13. GET /api/templates
@app.route("/api/templates", methods=["GET"])
def get_templates():
    try:
        from generation.template_manager import list_templates, ensure_seeded
        ensure_seeded()
        doc_type = request.args.get("document_type")
        return json_resp({"templates": list_templates(doc_type)})
    except Exception as e:
        logger.exception("get_templates failed")
        return json_resp({"error": str(e)}, 500)


# 14. POST /api/templates
@app.route("/api/templates", methods=["POST"])
def create_template():
    try:
        from generation.template_manager import save_user_template
        body          = request.get_json()
        name          = body.get("name", "").strip()
        document_type = body.get("document_type", "").strip()
        sections      = body.get("sections") or []
        if not name or not document_type or not sections:
            return json_resp({"error": "name, document_type and sections are required"}, 400)
        tmpl = save_user_template(
            name=name, document_type=document_type, sections=sections,
            system_instructions=body.get("system_instructions"),
            description=body.get("description"),
        )
        return json_resp({"template": tmpl.to_dict(), "message": "Template saved."}, 201)
    except Exception as e:
        logger.exception("create_template failed")
        return json_resp({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# PROJECT ENDPOINTS  (DB-backed via SQLAlchemy — consistent with function_app)
# ═════════════════════════════════════════════════════════════════════════════

import json as _json_mod

def _get_proj_or_404(session, project_id: str):
    from generation.db import Project as _P
    p = session.get(_P, project_id)
    if p is None:
        raise FileNotFoundError(f"Project '{project_id}' not found")
    return p

def _apply_fields(proj, body: dict) -> None:
    """Write body keys onto the ORM project object. Safe for partial updates."""
    scalar = [
        "project_code", "project_name", "business_unit", "business_priority",
        "problem_statement", "project_objective", "as_is_processes",
        "proposed_solution", "technical_landscape", "constraints", "risks",
        "estimated_cost_crores", "start_date", "end_date",
        "document_type", "output_format", "additional_instructions",
        "template_id", "status",
    ]
    for f in scalar:
        if f in body:
            setattr(proj, f, body[f])
    if "stakeholders" in body:
        v = body["stakeholders"]
        proj.stakeholders_json = _json_mod.dumps(v) if isinstance(v, list) else v
    if "document_ids" in body:
        v = body["document_ids"]
        proj.document_ids_json = _json_mod.dumps(v) if isinstance(v, list) else v


# 15. POST /api/extract-project-data
@app.route("/api/extract-project-data", methods=["POST"])
def extract_project_data():
    try:
        from api.extractor import extract_project_data as _extract
        body         = request.get_json() or {}
        document_ids = body.get("document_ids") or []
        if not document_ids:
            return json_resp({"error": "document_ids array is required"}, 400)
        result  = _extract(document_ids)
        filled  = result.get("filled_count", 0)
        total   = result.get("total_fields", 15)
        missing = len(result.get("missing_required", []))
        msg = (f"All fields populated from {len(document_ids)} document(s)."
               if missing == 0 else
               f"Extracted {filled}/{total} fields. {missing} required field(s) still needed.")
        return json_resp({**result, "document_count": len(document_ids), "message": msg})
    except FileNotFoundError as e:
        return json_resp({"error": str(e)}, 404)
    except RuntimeError as e:
        # LLM call failed — return 502 so the frontend knows it's a backend config issue,
        # not an extraction quality issue (which would look identical as HTTP 200 + empty fields)
        logger.error("extract-project-data LLM error: %s", e)
        return json_resp({"error": f"LLM extraction failed: {e}. Check MODEL_PROVIDER and API keys in .env."}, 502)
    except Exception as e:
        logger.exception("extract-project-data failed")
        return json_resp({"error": str(e)}, 500)


# 15b. POST /api/projects/draft  — create a draft project without field validation
# Used by the frontend to persist AI-extracted data immediately after extraction,
# before the user has filled all required fields. The project is always status="draft".
# Any subset of project fields may be sent; missing fields are left null/empty.
@app.route("/api/projects/draft", methods=["POST"])
def create_draft_project():
    try:
        from generation.db import Project as _P, DerivedData as _D, get_session
        body = request.get_json() or {}
        pid  = str(uuid4())
        proj = _P(project_id=pid)
        proj.status = "draft"
        _apply_fields(proj, body)  # safe: only sets keys present in body
        with get_session() as s:
            s.add(proj)
            s.add(_D(project_id=pid))
            s.commit()
        return json_resp({"project_id": pid, "status": "draft",
                          "message": "Draft project created."}, 201)
    except Exception as e:
        logger.exception("create_draft_project failed")
        return json_resp({"error": str(e)}, 500)


# 16. POST /api/projects  — create project (full validation, status → "ready")
@app.route("/api/projects", methods=["POST"])
def create_project():
    try:
        from generation.db import Project as _P, DerivedData as _D, get_session
        from models.project_schema import ProjectFormData
        from pydantic import ValidationError
        body = request.get_json() or {}
        try:
            form = ProjectFormData(**body)
        except ValidationError as ve:
            # Return each missing/blank field as a clear error message
            errors = [
                {"field": e["loc"][0] if e["loc"] else "unknown", "message": e["msg"]}
                for e in ve.errors()
            ]
            return json_resp({"error": "Validation failed", "fields": errors}, 422)
        pid  = str(uuid4())
        proj = _P(project_id=pid)
        proj.status = "ready"
        _apply_fields(proj, form.model_dump())
        with get_session() as s:
            s.add(proj)
            s.add(_D(project_id=pid))
            s.commit()
            saved_status = proj.status  # read inside session before it closes
        return json_resp({"project_id": pid, "status": saved_status,
                          "message": f"Project '{form.project_name}' saved."}, 201)
    except Exception as e:
        logger.exception("create_project failed")
        return json_resp({"error": str(e)}, 500)


# 17. GET /api/projects  — list all projects
@app.route("/api/projects", methods=["GET"])
def list_projects():
    try:
        from generation.db import Project as _P, get_session
        from sqlalchemy import or_, func as sqlfunc
        q      = (request.args.get("q")      or "").strip().lower()
        status = (request.args.get("status") or "").strip()
        with get_session() as s:
            qry = s.query(_P)
            if q:
                qry = qry.filter(or_(
                    sqlfunc.lower(_P.project_name).contains(q),
                    sqlfunc.lower(_P.project_code).contains(q),
                ))
            if status:
                qry = qry.filter(_P.status == status)
            rows = qry.order_by(_P.created_at.desc()).all()
            summaries = [p.to_summary_dict() for p in rows]
        return json_resp({"projects": summaries, "count": len(summaries)})
    except Exception as e:
        logger.exception("list_projects failed")
        return json_resp({"error": str(e)}, 500)


# 18a. GET /api/projects/<project_id>  — full project
@app.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    try:
        from generation.db import get_session
        with get_session() as s:
            return json_resp(_get_proj_or_404(s, project_id).to_full_dict())
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18b. PUT /api/projects/<project_id>  — full update
@app.route("/api/projects/<project_id>", methods=["PUT"])
def update_project(project_id):
    try:
        from generation.db import get_session
        body = request.get_json() or {}
        now  = datetime.utcnow()
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            _apply_fields(proj, body)
            proj.updated_at = now
            s.commit()
        return json_resp({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18c. PATCH /api/projects/<project_id>  — partial update / autosave
@app.route("/api/projects/<project_id>", methods=["PATCH"])
def patch_project(project_id):
    try:
        from generation.db import get_session
        body = request.get_json() or {}
        if not body:
            return json_resp({"error": "Request body is empty."}, 400)
        now = datetime.utcnow()
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            _apply_fields(proj, body)
            proj.updated_at = now
            s.commit()
        return json_resp({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18d. DELETE /api/projects/<project_id>
@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    try:
        from generation.db import get_session
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            s.delete(proj)
            s.commit()
        from flask import Response as _R
        return _R(status=204, headers=CORS_HEADERS)
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18e. GET /api/projects/<project_id>/data  — ingested + derived combined
@app.route("/api/projects/<project_id>/data", methods=["GET"])
def get_project_data(project_id):
    try:
        from generation.db import DerivedData as _D, get_session
        with get_session() as s:
            proj    = _get_proj_or_404(s, project_id)
            ingested = proj.to_ingested_dict()
            for k in ("project_id","status","job_id","created_at","updated_at",
                      "document_type","output_format","additional_instructions",
                      "document_ids","template_id"):
                ingested.pop(k, None)
            row = s.get(_D, project_id)
            if row:
                derived      = row.to_dict()
                generated_at = derived.pop("generated_at", None)
                derived.pop("project_id", None); derived.pop("updated_at", None)
            else:
                derived = {k:"" for k in [
                    "current_challenges","to_be_process","success_criteria",
                    "business_requirements","functional_requirements",
                    "non_functional_requirements","industry_benchmarks","workflow",
                    "analytics_requirements","systems_involved",
                    "data_sources","constraints_dependencies",
                ]}
                generated_at = None
        return json_resp({"project_id": project_id, "ingested": ingested,
                          "derived": derived, "derived_generated_at": generated_at})
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18f. PUT /api/projects/<project_id>/data/ingested  — save ingested edits
@app.route("/api/projects/<project_id>/data/ingested", methods=["PUT"])
def update_ingested_data(project_id):
    try:
        from generation.db import get_session
        body = request.get_json() or {}
        if not body:
            return json_resp({"error": "Request body is empty."}, 400)
        non_ing = {"status","job_id","document_type","output_format",
                   "additional_instructions","document_ids","template_id"}
        body = {k: v for k, v in body.items() if k not in non_ing}
        now = datetime.utcnow()
        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            _apply_fields(proj, body)
            proj.updated_at = now
            s.commit()
        return json_resp({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18g. PUT /api/projects/<project_id>/data/derived  — save derived edits
@app.route("/api/projects/<project_id>/data/derived", methods=["PUT"])
def update_derived_data(project_id):
    try:
        from generation.db import DerivedData as _D, get_session
        body = request.get_json() or {}
        if not body:
            return json_resp({"error": "Request body is empty."}, 400)
        DER_FIELDS = [
            "current_challenges","to_be_process","success_criteria",
            "business_requirements","functional_requirements",
            "non_functional_requirements","industry_benchmarks","workflow",
            "analytics_requirements","systems_involved",
            "data_sources","constraints_dependencies",
        ]
        mark_gen = bool(body.get("mark_as_generated", False))
        now = datetime.utcnow()
        with get_session() as s:
            _get_proj_or_404(s, project_id)   # existence check
            row = s.get(_D, project_id)
            if row is None:
                row = _D(project_id=project_id); s.add(row)
            for f in DER_FIELDS:
                if f in body: setattr(row, f, body[f])
            row.updated_at = now
            if mark_gen: row.generated_at = now
            s.commit()
        return json_resp({"project_id": project_id, "updated_at": now.isoformat()})
    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except Exception as e:
        return json_resp({"error": str(e)}, 500)


# 18h. POST /api/projects/<project_id>/derive-fields  — AI-derive 12 fields
@app.route("/api/projects/<project_id>/derive-fields", methods=["POST"])
def derive_project_fields(project_id):
    try:
        from generation.db import DerivedData as _D, get_session
        from generation.derive_fields import derive_project_fields as _derive

        with get_session() as s:
            proj = _get_proj_or_404(s, project_id)
            project_data = proj.to_ingested_dict()
            doc_ids = _json_mod.loads(proj.document_ids_json or "[]")

        derived = _derive(project_data, doc_ids)
        now     = datetime.utcnow()

        with get_session() as s:
            _get_proj_or_404(s, project_id)
            row = s.get(_D, project_id)
            if row is None:
                row = _D(project_id=project_id); s.add(row)
            for k, v in derived.items():
                if hasattr(row, k): setattr(row, k, v)
            row.generated_at = now
            row.updated_at   = now
            s.commit()

        populated = sum(1 for v in derived.values() if v)
        return json_resp({"project_id": project_id, "status": "ok",
                          "fields_populated": populated, "updated_at": now.isoformat(),
                          "message": f"{populated} fields derived by AI."})

    except FileNotFoundError:
        return json_resp({"error": f"Project '{project_id}' not found."}, 404)
    except RuntimeError as e:
        # LLM failure — both Gemini and Azure GPT-5 failed
        logger.error("derive-fields LLM error for project %s: %s", project_id, e)
        return json_resp({"error": f"AI derivation failed: {e}"}, 502)
    except Exception as e:
        logger.exception("derive-fields failed for project %s", project_id)
        return json_resp({"error": str(e)}, 500)


# 19. POST /api/generate/project/<project_id>  — start generation from saved project
@app.route("/api/generate/project/<project_id>", methods=["POST"])
def generate_from_project(project_id):
    """
    Start document generation for a saved project.
    Delegates entirely to generation_service.start_job_from_project() —
    which is the single source of truth shared with the ADK agent tool.
    """
    try:
        from generation.generation_service import start_job_from_project
        job = start_job_from_project(project_id)
        return json_resp({
            "job_id":   job["job_id"],
            "status":   job["status"],
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
        return json_resp({"error": str(e)}, 404)
    except ValueError as e:
        return json_resp({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("generate_from_project failed")
        return json_resp({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# CHAT STUDIO ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/api/chat/init", methods=["POST"])
def chat_init():
    """Create a new chat session + opening message. Body: { project_id, document_type, project_name? }"""
    try:
        body         = request.get_json() or {}
        project_id   = body.get("project_id", "").strip()
        doc_type     = body.get("document_type", "brd").strip()
        project_name = body.get("project_name", "").strip()
        if not project_id:
            return json_resp({"error": "project_id is required"}, 400)
        from api.chat_handler import init_session
        return json_resp(init_session(project_id, doc_type, project_name), 201)
    except Exception as e:
        logger.exception("chat/init failed")
        return json_resp({"error": str(e)}, 500)


@app.route("/api/chat/message", methods=["POST"])
def chat_message():
    """Send a user message and receive an assistant response. Body: { session_id, message, project_id?, document_type? }"""
    try:
        body       = request.get_json() or {}
        session_id = body.get("session_id", "").strip()
        message    = body.get("message", "").strip()
        project_id = body.get("project_id")
        doc_type   = body.get("document_type")
        if not session_id:
            return json_resp({"error": "session_id is required"}, 400)
        if not message:
            return json_resp({"error": "message is required"}, 400)
        from api.chat_handler import process_message
        return json_resp(process_message(session_id, message, project_id, doc_type))
    except Exception as e:
        logger.exception("chat/message failed")
        return json_resp({"error": str(e)}, 500)


@app.route("/api/chat/<session_id>/history", methods=["GET"])
def chat_history(session_id):
    """Return full ChatSession with all messages."""
    try:
        from api.chat_handler import get_history
        return json_resp(get_history(session_id))
    except ValueError as e:
        return json_resp({"error": str(e)}, 404)
    except Exception as e:
        logger.exception("chat_history failed")
        return json_resp({"error": str(e)}, 500)


@app.route("/api/templates/<template_id>/reseed", methods=["POST"])
def reseed_template_route(template_id):
    """Force re-seed a system template from its JSON file. Use after editing a template JSON."""
    try:
        from generation.template_manager import reseed_template
        ok = reseed_template(template_id)
        if not ok:
            return json_resp({"error": f"Template JSON not found: {template_id}.json"}, 404)
        return json_resp({"status": "reseeded", "template_id": template_id})
    except Exception as e:
        logger.exception("reseed_template failed")
        return json_resp({"error": str(e)}, 500)


@app.route("/api/chat/<session_id>/upload", methods=["POST"])
def chat_upload(session_id):
    """
    Upload a document via the chat UI.
    Parses the file, stores it, attaches to the session's project,
    and returns a chat-style confirmation response.
    Body: multipart/form-data with 'file' field.
    """
    try:
        from parsers.parser_factory import parse_document, SUPPORTED_EXTENSIONS
        from api.chat_handler import attach_document_to_session

        file_data = request.files.get("file")
        if not file_data:
            return json_resp({"error": "No file provided. Send as multipart field 'file'."}, 400)

        filename = file_data.filename or "upload"
        ext      = Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            return json_resp({"error": f"Unsupported file type '{ext}'.",
                              "supported": SUPPORTED_EXTENSIONS}, 415)

        raw = file_data.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return json_resp({"error": "File too large. Max 50 MB."}, 413)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)

        try:
            parsed_doc = parse_document(tmp_path)
            parsed_doc.source_filename = filename
            parsed_doc = _get_store().persist_all(parsed_doc, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        result = attach_document_to_session(session_id, parsed_doc.document_id, filename)
        return json_resp(result, 201)

    except ValueError as e:
        return json_resp({"error": str(e)}, 400)
    except Exception as e:
        logger.exception("chat_upload failed")
        return json_resp({"error": str(e)}, 500)


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print("  Intellidraft API Server")
    print("  -----------------------")
    print("  API    ->  http://localhost:7071/api")
    print("  Docs   ->  http://localhost:7071/api/docs")
    print("  Health ->  http://localhost:7071/api/health")
    print("  Stop   ->  Ctrl+C")
    print()
    # threaded=True allows concurrent poll requests while background generation runs
    app.run(host="0.0.0.0", port=7071, debug=False, use_reloader=False, threaded=True)
