"""
Document Processor Agent — Google ADK  (v2)
============================================

HOW TO RUN
----------
  cd "…\\Intellidraft"          ← the PARENT directory of Data_Ingestion\\
  adk web                       ← discovers Data_Ingestion as the agent app

  DO NOT run from inside Data_Ingestion\\ — that creates a doubled path and
  ADK will report "No root_agent found for 'Data_Ingestion'".

MODEL_PROVIDER (set in .env):
  azure_gpt5   → Azure GPT-5 project-pulse  ← ACTIVE
  gemini       → Google Gemini
  azure_openai → Any other Azure OpenAI deployment

Tools:
  0. list_artifacts_tool      — show what files have been uploaded this session
  1. parse_document_tool      — parse an uploaded file, save meta + vision analysis
  2. get_document_meta_tool   — fetch parsed meta by document_id
  3. list_elements_tool       — list elements by type (text / image / table)
  4. submit_user_inputs_tool  — attach user context to a parsed document
"""

from __future__ import annotations
import hashlib
import inspect
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap: add Data_Ingestion/ to sys.path so every relative import
# (parsers, models, storage, api …) works regardless of the CWD.
# ─────────────────────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent.parent.resolve()   # …/Data_Ingestion/
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# ─────────────────────────────────────────────────────────────────────────────
# Load .env
# Priority:  Data_Ingestion/.env  (ADK canonical location — must exist)
#         →  Data_Ingestion/agent/.env  (legacy / direct-run fallback)
# ─────────────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv  # noqa: E402
load_dotenv(dotenv_path=_BASE / ".env",                override=False)
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

os.environ["PYTHONUTF8"] = "1"

# ─────────────────────────────────────────────────────────────────────────────
# Standard imports — all resolved via the sys.path bootstrap above
# ─────────────────────────────────────────────────────────────────────────────
from google.adk.agents import LlmAgent          # noqa: E402
from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.adk.tools import ToolContext         # noqa: E402
from google.genai import types as genai_types   # noqa: E402  — for Part creation in callback

from parsers.parser_factory import parse_document           # noqa: E402
from storage.azure_storage  import get_storage_service      # noqa: E402
from models.meta_schema     import ParsedDocument, UserInputData  # noqa: E402

logger = logging.getLogger(__name__)

# Storage singleton — shared across all tool calls in one session
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = get_storage_service()
    return _store


# ─────────────────────────────────────────────────────────────────────────────
# ADK Tools
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
                    "No files uploaded yet. Click the paperclip (📎) icon in the "
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


async def parse_document_tool(filename: str, tool_context: ToolContext) -> dict:
    """
    Parse an uploaded document file (PDF, DOCX, PPTX, XLSX).
    The file must have been uploaded via the ADK web UI (paperclip / 📎 icon).
    Vision AI automatically analyses any images found (workflows, diagrams, charts).

    Args:
        filename: Exact filename as uploaded (e.g. 'requirements.pdf').
                  If unsure, call list_artifacts_tool first.

    Returns:
        dict with document_id, summary (element counts, image types found), storage path.
    """
    artifact = await tool_context.load_artifact(filename)
    if artifact is None:
        # Provide a helpful list of what IS available
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
        parsed_doc                 = parse_document(tmp_path)
        parsed_doc.source_filename = filename
        parsed_doc                 = _get_store().persist_all(parsed_doc, tmp_path)

        s      = parsed_doc.summary
        extras = []
        if s.has_workflows:
            extras.append("workflow/flowchart diagrams")
        if s.has_architecture:
            extras.append("architecture diagrams")
        if s.has_charts:
            extras.append("charts/graphs")

        vision_note = (
            f" Vision AI identified: {', '.join(extras)}." if extras else ""
        )

        return {
            "document_id":  parsed_doc.document_id,
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


async def get_document_meta_tool(document_id: str, tool_context: ToolContext) -> dict:
    """
    Retrieve the parsed meta summary for a document.
    Shows element counts, image types found, and content previews.

    Args:
        document_id: UUID returned by parse_document_tool.
    """
    try:
        meta = _get_store().get_meta_json(document_id)
        return {
            "document_id":   meta.get("document_id"),
            "source":        meta.get("source_filename"),
            "summary":       meta.get("summary"),
            "text_preview":  [
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
        meta = _get_store().get_meta_json(document_id)
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
    Attach user-provided context to a parsed document.
    Call this after parse_document_tool once you have collected project details.

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
        meta = _get_store().get_meta_json(document_id)
        doc  = ParsedDocument(**meta)
        doc.user_inputs = UserInputData(
            project_name             = project_name,
            document_type            = document_type,
            output_format            = output_format,
            stakeholders             = stakeholders or None,
            project_description      = project_description or None,
            additional_instructions  = additional_instructions or None,
            generation_mode          = generation_mode,
        )
        _get_store().save_meta_json(doc)
        _get_store().save_to_cosmos(doc)
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


# ─────────────────────────────────────────────────────────────────────────────
# Before-model callback — intercept file uploads, save to artifact store,
# then strip raw bytes from messages so Azure never sees them.
# ─────────────────────────────────────────────────────────────────────────────

# MIME type → file extension mapping for generated artifact filenames
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf":          "pdf",
    "application/msword":       "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":   "docx",
    "application/vnd.ms-powerpoint":                                             "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-excel":                                                  "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":         "xlsx",
}


async def _strip_file_content_callback(callback_context, llm_request):
    """
    Intercept every LLM call and handle inline_data / file_data Parts.

    WHY THIS EXISTS
    ---------------
    ADK 2.0.0 passes files uploaded via the web-UI paperclip as raw
    inline_data bytes inside the user message — it does NOT automatically
    save them to the ADK artifact service.  Two problems follow:

      1. ADK's lite_llm.py detects Azure as a _FILE_ID_REQUIRED_PROVIDER
         and tries  litellm.acreate_file(file=bytes, ...)  without a MIME
         type → Azure returns  400 Invalid file data: 'file_id'.

      2. tool_context.load_artifact() / list_artifacts() return nothing
         because the artifact service was never populated.

    WHAT WE DO
    ----------
    For every inline_data Part in every user message:

      a. Hash the first 4 KB to produce a stable 12-char key.
         On the FIRST occurrence we call  callback_context.save_artifact()
         to write the Part into the ADK artifact service under the name
           upload_<hash>.<ext>
         We record hash → filename in session state so subsequent LLM
         turns (which replay the full conversation history) skip the save.

      b. Replace the Part with a plain-text placeholder that tells the LLM
         the exact filename to pass to parse_document_tool().

    After this callback:
      • The LLM call goes to Azure with clean text-only content (no bytes).
      • list_artifacts_tool() returns the saved filename(s).
      • parse_document_tool(filename) can call load_artifact(filename)
        successfully.

    Returns None → ADK continues with the (now-modified) request.
    """
    if not hasattr(llm_request, "contents") or not llm_request.contents:
        return None

    # ── Restore the per-session hash→filename map from state ─────────────────
    # This prevents re-saving the same file on every subsequent LLM turn.
    try:
        artifact_map: dict[str, str] = dict(
            callback_context.state.get("_uploaded_files", {}) or {}
        )
    except Exception:
        artifact_map = {}

    map_updated = False

    for i, content in enumerate(llm_request.contents):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None)
        if not parts:
            continue

        new_parts: list = []
        changed   = False

        for part in parts:

            # ── inline_data  (file uploaded via the web-UI paperclip) ────────
            if getattr(part, "inline_data", None) is not None:
                mime = getattr(part.inline_data, "mime_type", None) or "application/octet-stream"
                data = getattr(part.inline_data, "data", b"") or b""

                # Stable key: hash of first 4 KB (fast, unique enough for docs)
                file_key = hashlib.md5(data[:4096]).hexdigest()[:12]

                if file_key in artifact_map:
                    # Already saved in a previous turn — just reuse the name
                    filename = artifact_map[file_key]
                    logger.info("[Callback] File already in artifact store as '%s'", filename)
                else:
                    # First time we see this file — save it to the artifact service
                    ext = _MIME_TO_EXT.get(mime) or (mime.split("/")[-1][:8]) or "bin"
                    filename = f"upload_{file_key}.{ext}"

                    save_fn = getattr(callback_context, "save_artifact", None)
                    if save_fn and callable(save_fn):
                        try:
                            result = save_fn(filename, part)
                            # Handle both sync and async implementations
                            if inspect.isawaitable(result):
                                await result
                            logger.info(
                                "[Callback] Saved inline_data as artifact '%s' (mime=%s)",
                                filename, mime,
                            )
                        except Exception as exc:
                            logger.warning("[Callback] save_artifact('%s') failed: %s", filename, exc)
                    else:
                        logger.warning(
                            "[Callback] callback_context has no save_artifact — "
                            "tools will not be able to load '%s'", filename,
                        )

                    artifact_map[file_key] = filename
                    map_updated = True

                # Replace the raw-bytes Part with a text instruction for the LLM
                new_parts.append(genai_types.Part(
                    text=(
                        f"[Uploaded file saved as '{filename}' (MIME: {mime}). "
                        f"Call list_artifacts_tool() to confirm, then "
                        f"parse_document_tool('{filename}') to extract its contents.]"
                    )
                ))
                changed = True
                logger.info("[Callback] Stripped inline_data (mime=%s) from user message", mime)

            # ── file_data  (Google Files API reference — rare with ADK web) ──
            elif getattr(part, "file_data", None) is not None:
                uri  = getattr(part.file_data, "file_uri", "") or ""
                name = uri.rsplit("/", 1)[-1] if uri else "uploaded_file"
                new_parts.append(genai_types.Part(
                    text=(
                        f"[File '{name}' attached via Files API. "
                        f"Call parse_document_tool('{name}') to process it.]"
                    )
                ))
                changed = True
                logger.info("[Callback] Stripped file_data (uri=%s) from user message", uri)

            else:
                new_parts.append(part)

        if changed:
            # Mutate in-place where possible; rebuild the Content object if not
            try:
                content.parts = new_parts
            except Exception:
                try:
                    new_content = genai_types.Content(role=content.role, parts=new_parts)
                    llm_request.contents[i] = new_content
                except Exception as exc:
                    logger.warning("[Callback] Could not replace content parts: %s", exc)

    # ── Persist the updated hash→filename map for future turns ───────────────
    if map_updated:
        try:
            callback_context.state["_uploaded_files"] = artifact_map
        except Exception as exc:
            logger.warning("[Callback] Could not persist artifact map to state: %s", exc)

    return None   # continue with the (now-clean) request


# ─────────────────────────────────────────────────────────────────────────────
# Model selection
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER = os.getenv("MODEL_PROVIDER", "azure_gpt5").lower()

GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
AZURE_GPT5_KEY  = os.getenv("AZURE_GPT5_OPENAI_API_KEY",      "")
AZURE_GPT5_BASE = os.getenv("AZURE_GPT5_OPENAI_ENDPOINT",     "")
AZURE_GPT5_VER  = os.getenv("AZURE_GPT5_API_VERSION",         "2024-12-01-preview")
AZURE_GPT5_DEP  = os.getenv("AZURE_GPT5_MODEL_DEPLOYMENT_ID", "project-pulse-gpt-5")
AZURE_OAI_KEY   = os.getenv("AZURE_OPENAI_API_KEY",           "")
AZURE_OAI_BASE  = os.getenv("AZURE_OPENAI_ENDPOINT",          "")
AZURE_OAI_VER   = os.getenv("AZURE_OPENAI_API_VERSION",       "2024-02-01")
AZURE_OAI_DEP   = os.getenv("AZURE_OPENAI_DEPLOYMENT",        "gpt-4o")


def _get_model():
    if PROVIDER == "gemini":
        print(f"[Agent] Model: Gemini -> {GEMINI_MODEL}")
        return GEMINI_MODEL

    elif PROVIDER == "azure_gpt5":
        if not AZURE_GPT5_KEY or not AZURE_GPT5_BASE:
            raise ValueError(
                "AZURE_GPT5_OPENAI_API_KEY and AZURE_GPT5_OPENAI_ENDPOINT "
                "must be set in Data_Ingestion/.env"
            )
        os.environ.update({
            "AZURE_API_KEY":     AZURE_GPT5_KEY,
            "AZURE_API_BASE":    AZURE_GPT5_BASE,
            "AZURE_API_VERSION": AZURE_GPT5_VER,
        })
        model_str = f"azure/{AZURE_GPT5_DEP}"
        print(f"[Agent] Model: Azure GPT-5 -> {model_str}")
        print(f"[Agent] Endpoint: {AZURE_GPT5_BASE}  API-ver: {AZURE_GPT5_VER}")
        return LiteLlm(
            model       = model_str,
            api_key     = AZURE_GPT5_KEY,
            api_base    = AZURE_GPT5_BASE,
            api_version = AZURE_GPT5_VER,
        )

    elif PROVIDER == "azure_openai":
        if not AZURE_OAI_KEY or not AZURE_OAI_BASE:
            raise ValueError(
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT "
                "must be set in Data_Ingestion/.env"
            )
        os.environ.update({
            "AZURE_API_KEY":     AZURE_OAI_KEY,
            "AZURE_API_BASE":    AZURE_OAI_BASE,
            "AZURE_API_VERSION": AZURE_OAI_VER,
        })
        model_str = f"azure/{AZURE_OAI_DEP}"
        print(f"[Agent] Model: Azure OpenAI -> {model_str}")
        return LiteLlm(
            model       = model_str,
            api_key     = AZURE_OAI_KEY,
            api_base    = AZURE_OAI_BASE,
            api_version = AZURE_OAI_VER,
        )

    raise ValueError(
        f"Unknown MODEL_PROVIDER='{PROVIDER}'. "
        "Use: gemini | azure_gpt5 | azure_openai"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent instruction
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTION = f"""
You are the Intellidraft Document Processing Agent. Today: {date.today().isoformat()}.

Your role is to help users:
1. Upload source documents (PDF, DOCX, PPTX, XLSX) via the paperclip icon
2. Extract all content — text, images (with AI-generated descriptions for diagrams and
   workflows), and tables
3. Collect project context from the user
4. Prepare everything for document generation (BRD, RFP, SOW, Proposal, etc.)

IMPORTANT NOTES ABOUT FILE UPLOADS:
- The ADK web UI artifact panel may not visually refresh after upload — this is a UI
  display quirk, NOT an error. The file IS stored.
- Always call list_artifacts_tool at the start of a conversation to see what files
  are actually available.
- To upload MULTIPLE documents: upload one file, ask me to parse it, then upload the
  next file and ask me to parse it. Each file is processed independently.

WORKFLOW — follow this exact sequence:

STEP 0 — CHECK WHAT'S UPLOADED
  At the start of every conversation (or when the user says they uploaded something),
  call list_artifacts_tool first.
  Tell the user what files were found and ask which one to parse (or parse the obvious
  one if there is only one).

STEP 1 — PARSE
  Call parse_document_tool with the exact filename shown by list_artifacts_tool.
  Report what was found: text blocks, images (note diagrams/workflows/charts), tables.
  If there are multiple uploaded files, ask the user which to process or process each
  one in sequence.

STEP 2 — SHOW CONTENTS (if the user asks)
  Use list_elements_tool to preview specific element types.
  For images, always show the ai_description and image_type — this tells the user what
  diagrams were understood.

STEP 3 — COLLECT PROJECT CONTEXT
  Ask the user for:
    • Project name
    • Output document type (BRD, RFP, SOW, Proposal, Technical Specification, Scope Document)
    • Output format (Word / PDF / Markdown)
    • Stakeholders
    • Project description
    • Business problem this project solves
    • Any special instructions for the AI

STEP 4 — SAVE INPUTS
  Call submit_user_inputs_tool with everything collected.

STEP 5 — CONFIRM & NEXT STEPS
  Tell the user:
    • Their document_id (needed for the generation API)
    • What content was extracted (highlight any workflows/architecture diagrams found)
    • That they can now call POST /api/generate/start with the document_id to begin generation
"""


# ─────────────────────────────────────────────────────────────────────────────
# Root agent — ADK discovers this via:
#   (b) Data_Ingestion/__init__.py  → from .agent import root_agent
#   (c) Data_Ingestion/agent/__init__.py → from .agent import root_agent
# ─────────────────────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    name        = "doc_processor",
    model       = _get_model(),
    description = (
        "Parses uploaded documents (PDF, DOCX, PPTX, XLSX), extracts text/images/tables, "
        "runs vision AI on diagrams and architecture charts, "
        "and collects user project context for AI document generation."
    ),
    instruction = INSTRUCTION,
    tools       = [
        list_artifacts_tool,
        parse_document_tool,
        get_document_meta_tool,
        list_elements_tool,
        submit_user_inputs_tool,
    ],
    # Strip raw file bytes before the LLM call — prevents ADK from uploading
    # files to Azure Files API (which fails with MIME-type 400 errors).
    # Our parse_document_tool handles the actual file reading locally.
    before_model_callback = _strip_file_content_callback,
)
