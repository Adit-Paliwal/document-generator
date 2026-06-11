"""
DocParser Tools
===============
ADK tools for DocParserAgent — Agent 1.

These tools wrap the document parsing pipeline:
  parsers/parser_factory.py  — PDF / DOCX / PPTX / XLSX parsing
  storage/azure_storage.py   — blob / local file storage
  models/meta_schema.py      — ParsedDocument + UserInputData schemas

The Vision AI callback (_strip_file_content_callback) lives in agent.py —
it intercepts raw file uploads BEFORE the LLM call and saves them to the
ADK artifact service so these tools can retrieve them by filename.

Tools
-----
  list_artifacts_tool     — list uploaded files in the current session
  parse_document_tool     — parse an uploaded document + run Vision AI
  get_document_meta_tool  — fetch parsed metadata by document_id
  list_elements_tool      — list extracted elements by type (text/image/table)
  submit_user_inputs_tool — attach user project context to a parsed document
"""

from __future__ import annotations
import logging
import os
import sys
import tempfile
from pathlib import Path

# ── Bootstrap: add Data_Ingestion/ to sys.path ───────────────────────────────
# agents/doc_parser/tools.py is 3 levels deep inside Data_Ingestion/
_BASE = Path(__file__).parent.parent.parent.resolve()   # → …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from google.adk.tools import ToolContext   # noqa: E402

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — list_artifacts_tool
# ─────────────────────────────────────────────────────────────────────────────

async def list_artifacts_tool(tool_context: ToolContext) -> dict:
    """
    List all files that have been uploaded to the current session.
    Call this at the start of every conversation so you know what files are
    available for parsing. The ADK web UI's artifact panel may not refresh
    visually, but all uploaded files are stored and accessible.

    Returns:
        dict with 'files' (list of filenames) and a human-readable 'message'.
    """
    try:
        filenames = await tool_context.list_artifacts()
        if not filenames:
            return {
                "files":   [],
                "message": (
                    "No files uploaded yet. Click the paperclip (\U0001f4ce) icon in the "
                    "chat input to upload a PDF, DOCX, PPTX, or XLSX file."
                ),
            }
        return {
            "files":   filenames,
            "count":   len(filenames),
            "message": (
                f"Found {len(filenames)} uploaded file(s): {', '.join(filenames)}. "
                "To parse a file, say 'parse <filename>'."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — parse_document_tool
# ─────────────────────────────────────────────────────────────────────────────

async def parse_document_tool(filename: str, tool_context: ToolContext) -> dict:
    """
    Parse an uploaded document file (PDF, DOCX, PPTX, XLSX).
    The file must have been uploaded via the ADK web UI (paperclip icon).
    Vision AI automatically analyses any images found (workflows, diagrams, charts).

    Args:
        filename: Exact filename as uploaded (e.g. 'requirements.pdf').
                  If unsure, call list_artifacts_tool first.

    Returns:
        dict with document_id, summary (element counts, image types found),
        and storage path. The document_id is also saved in session state as
        'last_parsed_document_id' so the Orchestrator can access it without
        the user having to repeat it.
    """
    from parsers.parser_factory import parse_document
    from storage.azure_storage  import get_storage_service

    artifact = await tool_context.load_artifact(filename)
    if artifact is None:
        try:
            available = await tool_context.list_artifacts()
        except Exception:
            available = []
        hint = (
            f" Available files: {', '.join(available)}" if available
            else " No files have been uploaded yet — use the paperclip icon first."
        )
        return {"error": f"File '{filename}' not found in this session.{hint}"}

    raw_bytes = artifact.inline_data.data
    ext       = os.path.splitext(filename)[1].lower()

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(raw_bytes)
        tmp_path = Path(tmp.name)

    try:
        store      = get_storage_service()
        parsed_doc = parse_document(tmp_path)
        parsed_doc.source_filename = filename
        parsed_doc = store.persist_all(parsed_doc, tmp_path)

        s      = parsed_doc.summary
        extras = []
        if s.has_workflows:    extras.append("workflow/flowchart diagrams")
        if s.has_architecture: extras.append("architecture diagrams")
        if s.has_charts:       extras.append("charts/graphs")

        vision_note = (
            f" Vision AI identified: {', '.join(extras)}." if extras else ""
        )

        # ── Save document IDs in session state for the Orchestrator ───────────
        doc_id       = parsed_doc.document_id
        existing_ids = list(tool_context.state.get("parsed_document_ids") or [])
        if doc_id not in existing_ids:
            existing_ids.append(doc_id)
        tool_context.state["parsed_document_ids"]     = existing_ids
        tool_context.state["last_parsed_document_id"] = doc_id

        return {
            "document_id":  doc_id,
            "filename":     filename,
            "storage_path": parsed_doc.blob_base_path,
            "summary":      s.model_dump(),
            "message": (
                f"Document parsed successfully. "
                f"Found {s.total_text_elements} text blocks, "
                f"{s.total_images} images ({s.images_analyzed} AI-analysed), "
                f"{s.total_tables} tables.{vision_note}"
            ),
        }
    except Exception as e:
        logger.exception("parse_document_tool failed")
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        tmp_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — get_document_meta_tool
# ─────────────────────────────────────────────────────────────────────────────

async def get_document_meta_tool(document_id: str, tool_context: ToolContext) -> dict:
    """
    Retrieve the parsed metadata summary for a document.
    Shows element counts, image types found, and content previews.

    Args:
        document_id: UUID returned by parse_document_tool.
    """
    try:
        from storage.azure_storage import get_storage_service
        from models.meta_schema    import ParsedDocument

        meta = get_storage_service().get_meta_json(document_id)
        return {
            "document_id":  meta.get("document_id"),
            "source":       meta.get("source_filename"),
            "summary":      meta.get("summary"),
            "text_preview": [
                {"ref": e["ref"], "preview": e["content"][:100]}
                for e in meta.get("text_elements", [])[:5]
            ],
            "tables": [
                {"ref": e["ref"], "caption": e.get("caption"), "rows": e.get("row_count")}
                for e in meta.get("table_elements", [])
            ],
            "images": [
                {
                    "ref":            e["ref"],
                    "type":           e.get("image_type"),
                    "ai_description": e.get("ai_description"),
                    "caption":        e.get("caption"),
                }
                for e in meta.get("image_elements", [])
            ],
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — list_elements_tool
# ─────────────────────────────────────────────────────────────────────────────

async def list_elements_tool(
    document_id:  str,
    element_type: str,
    tool_context: ToolContext,
) -> dict:
    """
    List extracted elements of a specific type from a parsed document.

    Args:
        document_id:  UUID of the document.
        element_type: One of 'text', 'image', or 'table'.
    """
    try:
        from storage.azure_storage import get_storage_service
        from models.meta_schema    import ParsedDocument

        meta = get_storage_service().get_meta_json(document_id)
        doc  = ParsedDocument(**meta)

        if element_type == "text":
            return {"elements": [
                {
                    "ref":     e.ref,
                    "heading": e.heading_level,
                    "preview": e.content[:120],
                    "words":   e.word_count,
                }
                for e in doc.text_elements
            ]}

        elif element_type == "image":
            return {"elements": [
                {
                    "ref":            e.ref,
                    "type":           e.image_type,
                    "ai_description": e.ai_description,
                    "caption":        e.caption,
                    "key_elements":   e.key_elements,
                    "location":       e.blob_url or e.local_path,
                }
                for e in doc.image_elements
            ]}

        elif element_type == "table":
            return {"elements": [
                {
                    "ref":     e.ref,
                    "caption": e.caption,
                    "rows":    e.row_count,
                    "cols":    e.col_count,
                    "preview": e.markdown[:300],
                }
                for e in doc.table_elements
            ]}

        else:
            return {"error": "element_type must be 'text', 'image', or 'table'"}

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — submit_user_inputs_tool
# ─────────────────────────────────────────────────────────────────────────────

async def submit_user_inputs_tool(
    document_id:             str,
    project_name:            str,
    document_type:           str,
    output_format:           str,
    tool_context:            ToolContext,
    stakeholders:            str = "",
    project_description:     str = "",
    additional_instructions: str = "",
    generation_mode:         str = "Complete (single pass)",
) -> dict:
    """
    Attach user-provided project context to a parsed document.
    Call this after parse_document_tool once you have collected all project details.

    Args:
        document_id:             UUID from parse_document_tool.
        project_name:            Name of the project.
        document_type:           Type of document to generate (BRD, RFP, SOW, Proposal, etc.).
        output_format:           'Word (.docx)', 'PDF', or 'Markdown'.
        stakeholders:            Comma-separated stakeholder names.
        project_description:     Brief project description.
        additional_instructions: Extra instructions for the LLM generator.
        generation_mode:         'Complete (single pass)' or 'Section by section'.
    """
    try:
        from storage.azure_storage import get_storage_service
        from models.meta_schema    import ParsedDocument, UserInputData

        store = get_storage_service()
        meta  = store.get_meta_json(document_id)
        doc   = ParsedDocument(**meta)
        doc.user_inputs = UserInputData(
            project_name            = project_name,
            document_type           = document_type,
            output_format           = output_format,
            stakeholders            = stakeholders or None,
            project_description     = project_description or None,
            additional_instructions = additional_instructions or None,
            generation_mode         = generation_mode,
        )
        store.save_meta_json(doc)
        store.save_to_cosmos(doc)
        return {
            "document_id": document_id,
            "status":      "ready_for_generation",
            "message": (
                f"Inputs saved for '{project_name}'. "
                f"Document type: {document_type}. Output: {output_format}. "
                "Ready for generation — call the generation API with this document_id."
            ),
        }
    except Exception as e:
        logger.exception("submit_user_inputs_tool failed")
        return {"error": str(e)}
