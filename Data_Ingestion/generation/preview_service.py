"""
preview_service.py
==================
Production-grade LibreOffice document preview for IntelliDraft.

How it works (synchronous — the Celery/Redis async path was retired with the
Docker/Cloud Run deployment; Databricks Apps runs a single process)
------------------------------------------------------------------
1. Client calls GET /api/generate/{job_id}/preview/html
2. In-memory cache hit → return HTML immediately
3. Cache miss → convert DOCX→HTML via LibreOffice headless in the request
   thread (unique -env:UserInstallation per conversion so parallel requests
   don't hit profile locks); falls back to a markdown2 renderer when
   LibreOffice is not installed (e.g. on Databricks).
4. Any PATCH to a section calls invalidate_preview_cache(job_id) so the next
   request re-converts.

NOTE: the React SPA renders previews client-side from section markdown; this
endpoint chiefly serves the legacy chat.html preview panel.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

LO_TIMEOUT = int(os.environ.get("LO_CONVERT_TIMEOUT", "90"))    # seconds

# ── In-process deduplication state ───────────────────────────────────────────
# Prevents multiple simultaneous conversions for the same job regardless of
# how many API calls arrive concurrently (polling, React auto-trigger, etc.).
#
# _preview_cache : job_id → HTML string
# _inflight      : job_id → "sync" sentinel while a conversion is running
# _inflight_lock : guards both dicts
_preview_cache:  dict[str, str]  = {}
_inflight:       dict[str, str]  = {}   # value = task_id or "sync"
_inflight_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public API — called from the FastAPI routes (main.py)
# ─────────────────────────────────────────────────────────────────────────────

def pregenerate_preview(job_id: str) -> None:
    """
    Called by the generation service when a job finishes.
    Kicks off the DOCX→HTML conversion in a daemon thread so the result
    is cached before the React client ever calls the preview endpoint.
    Safe to call multiple times — the in-flight guard prevents duplicate runs.
    """
    def _warmup() -> None:
        try:
            result = get_or_submit_preview(job_id)
            logger.info("[preview] Pre-warmup job %s → status=%s", job_id, result.get("status"))
        except Exception as exc:
            logger.warning("[preview] Pre-warmup failed (non-fatal) for job %s: %s", job_id, exc)

    t = threading.Thread(target=_warmup, daemon=True, name=f"preview-warmup-{job_id[:8]}")
    t.start()


def get_or_submit_preview(job_id: str) -> dict:
    """
    Returns one of:
      {"status": "ready",   "html": "<html>…", "cached": bool}
      {"status": "pending", "task_id": "…",    "poll_url": "…"}
      {"status": "error",   "error": "…"}
    """
    return _sync_preview(job_id)


def poll_preview_status(job_id: str, task_id: str) -> dict:
    """
    LEGACY endpoint shim — the Celery worker path was retired. Serves the
    in-memory cache when the conversion already finished; otherwise tells the
    caller to poll the (synchronous) /preview/html endpoint instead.
    """
    with _inflight_lock:
        cached = _preview_cache.get(job_id)
        still_running = _inflight.get(job_id) == "sync"
    if cached:
        return {"status": "ready", "html": cached, "cached": True}
    if still_running:
        return {"status": "pending", "state": "CONVERTING",
                "poll_url": f"/api/generate/{job_id}/preview/html"}
    return {"status": "pending", "state": "NOT_STARTED",
            "poll_url": f"/api/generate/{job_id}/preview/html"}


def invalidate_preview_cache(job_id: str) -> None:
    """
    Delete the cached preview for this job so the next request re-converts.
    Call this after any section is patched (manual edit) or regenerated.
    """
    with _inflight_lock:
        _preview_cache.pop(job_id, None)
        _inflight.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Core conversion — LibreOffice headless DOCX→HTML
# ─────────────────────────────────────────────────────────────────────────────

def convert_job_to_html(job_id: str) -> str:
    """
    Export job sections as DOCX and convert to self-contained HTML via LibreOffice.
    Each call uses a unique LO user-profile directory so multiple conversions
    can run in parallel without lock conflicts.

    Returns the HTML string (CSS and small images are inlined).
    Raises on any failure — the caller falls back to the markdown2 renderer.
    """
    from generation.doc_writer import export_job_to_temp

    with tempfile.TemporaryDirectory(prefix="intellidraft_preview_") as work_dir:
        work_path = Path(work_dir)

        # 1. Write DOCX to the temp directory
        docx_path = export_job_to_temp(job_id, work_path)
        logger.info("[preview] Exported DOCX for job %s → %s", job_id, docx_path.name)

        # 2. Unique LibreOffice user profile — enables parallel execution
        lo_profile = work_path / f"lo_profile_{uuid.uuid4().hex}"
        lo_profile.mkdir()

        # 3. Run LibreOffice headless conversion
        soffice = _find_soffice()
        cmd = [
            soffice,
            f"-env:UserInstallation=file:///{lo_profile.as_posix()}",
            "--headless",
            "--norestore",
            "--convert-to", "html:HTML (StarWriter)",
            "--outdir", str(work_path),
            str(docx_path),
        ]
        logger.debug("[preview] Running: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            timeout=LO_TIMEOUT,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice exited with code {result.returncode}. "
                f"stderr: {result.stderr[:600]}"
            )

        # 4. Find the HTML output file
        html_files = list(work_path.glob("*.html"))
        if not html_files:
            raise RuntimeError(
                f"LibreOffice produced no HTML. "
                f"stdout: {result.stdout[:300]} stderr: {result.stderr[:300]}"
            )

        raw_html = html_files[0].read_text(encoding="utf-8", errors="replace")
        logger.info(
            "[preview] Converted %s → HTML (%d bytes)", docx_path.name, len(raw_html)
        )

        # 5. Inline external assets (LO may produce a companion .css and image files)
        return _inline_assets(raw_html, work_path)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sync_preview(job_id: str) -> dict:
    """
    Local-dev fallback: try LibreOffice; fall back to markdown2 HTML if LO is absent.

    Deduplication (sync mode):
      - If HTML is already cached in _preview_cache → return immediately (no conversion).
      - If a conversion is already running (flag == "sync") → return pending so the
        caller retries; they will hit the cache once the running conversion finishes.
      - Otherwise → set the flag, run the conversion, cache the result, clear the flag.
    """
    # ── Cache hit ─────────────────────────────────────────────────────────────
    with _inflight_lock:
        cached = _preview_cache.get(job_id)
        if cached:
            return {"status": "ready", "html": cached, "cached": True}

        # ── Another thread is already converting → tell caller to retry ──────
        # Return an explicit poll_url + retry hint so ANY client (React, etc.)
        # knows to re-GET the same /preview/html endpoint until status=="ready".
        # (Sync mode has no Celery task_id — the poll target is the html endpoint.)
        if _inflight.get(job_id) == "sync":
            return {
                "status":         "pending",
                "cached":         False,
                "poll_url":       f"/api/generate/{job_id}/preview/html",
                "retry_after_ms": 1500,
            }

        # ── Claim the conversion slot ─────────────────────────────────────────
        _inflight[job_id] = "sync"

    try:
        html = convert_job_to_html(job_id)
        html = _inject_section_handlers(html, job_id)
        with _inflight_lock:
            _preview_cache[job_id] = html
        return {"status": "ready", "html": html, "cached": False}
    except EnvironmentError:
        # LibreOffice not installed — render via markdown2 instead
        result = _markdown2_preview(job_id)
        if result.get("status") == "ready":
            with _inflight_lock:
                _preview_cache[job_id] = result["html"]
        return result
    except Exception as e:
        logger.exception("[preview] Sync conversion failed for job %s", job_id)
        try:
            result = _markdown2_preview(job_id)
            if result.get("status") == "ready":
                with _inflight_lock:
                    _preview_cache[job_id] = result["html"]
            return result
        except Exception:
            return {"status": "error", "error": str(e)}
    finally:
        with _inflight_lock:
            if _inflight.get(job_id) == "sync":
                _inflight.pop(job_id, None)


def _markdown2_preview(job_id: str) -> dict:
    """
    Convert the assembled document markdown to HTML using markdown2.
    No LibreOffice or Celery required — works everywhere.
    Activated automatically when LibreOffice is not installed.
    """
    try:
        import markdown2
        from generation.doc_writer import assemble_preview
        preview = assemble_preview(job_id)
        md      = preview.get("markdown", "")
        body    = markdown2.markdown(
            md,
            extras=["tables", "fenced-code-blocks", "break-on-newline", "strike"],
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>"
            "body{font-family:Arial,sans-serif;max-width:900px;margin:40px auto;"
            "padding:0 24px;line-height:1.65;color:#1a1a2e;}"
            "h1{font-size:22px;border-bottom:2px solid #5c6bc0;padding-bottom:8px;margin-bottom:18px;}"
            "h2{font-size:18px;color:#3f51b5;margin-top:32px;margin-bottom:10px;}"
            "h3{font-size:15px;color:#283593;margin-top:22px;}"
            "p{margin:8px 0;}"
            "table{border-collapse:collapse;width:100%;margin:14px 0;}"
            "th,td{border:1px solid #c5cae9;padding:8px 12px;text-align:left;}"
            "th{background:#e8eaf6;font-weight:600;}"
            "code{background:#f5f5f5;padding:2px 5px;border-radius:3px;font-size:12.5px;}"
            "hr{border:none;border-top:1px solid #e0e0e0;margin:20px 0;}"
            "blockquote{border-left:3px solid #5c6bc0;margin:0;padding:4px 16px;color:#555;}"
            "ul,ol{padding-left:22px;}li{margin:3px 0;}"
            "</style></head><body>"
            + body
            + "</body></html>"
        )
        html = _inject_section_handlers(html, job_id)
        return {"status": "ready", "html": html, "cached": False, "renderer": "markdown2"}
    except Exception as e:
        logger.exception("[preview] markdown2 fallback failed for job %s", job_id)
        return {"status": "error", "error": str(e)}


def _inline_assets(html: str, work_dir: Path) -> str:
    """
    Make the HTML self-contained:
    - Replace linked <link rel=stylesheet> with inline <style>
    - Replace img src="…" for small files (<1 MB) with base64 data URIs
    """
    # Inline linked stylesheets
    def _replace_css(m):
        href = m.group(1)
        css_path = work_dir / href
        if css_path.exists():
            css = css_path.read_text(encoding="utf-8", errors="replace")
            return f"<style>{css}</style>"
        return m.group(0)

    html = re.sub(
        r'<link[^>]+href=["\']([^"\']+\.css)["\'][^>]*/?>', _replace_css, html, flags=re.IGNORECASE
    )

    # Inline small images as base64 data URIs
    def _replace_img(m):
        src = m.group(1)
        img_path = work_dir / src
        if img_path.exists() and img_path.stat().st_size < 1_048_576:
            ext  = img_path.suffix.lstrip(".").lower()
            mime = {
                "png":  "image/png",
                "jpg":  "image/jpeg",
                "jpeg": "image/jpeg",
                "gif":  "image/gif",
                "svg":  "image/svg+xml",
                "bmp":  "image/bmp",
            }.get(ext, "image/png")
            b64 = base64.b64encode(img_path.read_bytes()).decode()
            return f'src="data:{mime};base64,{b64}"'
        return m.group(0)

    html = re.sub(
        r'src=["\']([^"\']+\.(png|jpg|jpeg|gif|svg|bmp))["\']',
        _replace_img, html, flags=re.IGNORECASE
    )

    return html


def _find_soffice() -> str:
    """
    Locate the LibreOffice soffice binary across common installation paths.
    Raises EnvironmentError with setup instructions if not found.
    """
    candidates = [
        "soffice",                                                # on PATH (Linux Docker)
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice7.6/program/soffice",
        "/opt/libreoffice/program/soffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",     # Windows default
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
    ]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
        if os.path.isfile(c):
            return c

    raise EnvironmentError(
        "LibreOffice (soffice) not found — install it and add it to PATH, "
        "or rely on the built-in markdown2 preview fallback."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section click-handler injection
# ─────────────────────────────────────────────────────────────────────────────

def _inject_section_handlers(html: str, job_id: str) -> str:
    """
    Post-process LibreOffice HTML to enable true inline editing.

    Injects a script that:
    - Makes section headings clickable (hover highlight + pointer cursor)
    - On click: signals parent to fetch section content, then shows
      a contenteditable div directly in the document (replacing the
      LibreOffice-rendered paragraphs for that section)
    - Sticky amber toolbar appears with Save / Cancel buttons
    - On Save: sends content back to parent via postMessage → parent
      calls PATCH /section/{id} and reloads the preview

    Bidirectional postMessage protocol:
      iframe → parent : { type: 'intellidraft_section_click', section_id, title }
      parent → iframe : { type: 'intellidraft_section_content', section_id, content }
      iframe → parent : { type: 'intellidraft_save_section', section_id, content }
      parent → iframe : { type: 'intellidraft_save_complete' }
      parent → iframe : { type: 'intellidraft_save_error', error }

    Safe to call on error (returns html unchanged).
    """
    import json as json_mod
    from generation.db import GenerationJob, get_session as _gs

    try:
        with _gs() as session:
            job = session.get(GenerationJob, job_id)
            if not job or not job.sections:
                return html
            section_map = {
                sec.section_title: sec.section_id
                for sec in job.sections
            }
    except Exception:
        return html

    map_json = json_mod.dumps(section_map)

    script = (
        "<script>\n"
        "(function(){\n"
        "var SM=" + map_json + ";\n"          # section_title → section_id
        "var _sid=null,_hel=null,_orig=[];\n"  # active section state

        # ── sticky edit toolbar ──────────────────────────────────────────
        "function _bar(){return document.getElementById('_id_bar');}\n"
        "function _showBar(title){\n"
        "  var b=_bar();\n"
        "  if(!b){\n"
        "    b=document.createElement('div');\n"
        "    b.id='_id_bar';\n"
        "    b.style.cssText='position:sticky;top:0;z-index:9999;background:#fff3cd;"
        "border-bottom:2px solid #f0ad4e;padding:8px 16px;display:flex;"
        "align-items:center;gap:10px;font-family:sans-serif;box-shadow:0 2px 6px rgba(0,0,0,.12);';\n"
        "    b.innerHTML='<span style=\"font-size:13px;font-weight:600;color:#856404\">"
        "&#9998; Editing: <em id=\"_id_bar_t\"></em></span>'\n"
        "      +'<button id=\"_id_sav\" style=\"margin-left:auto;background:#5c6bc0;"
        "color:#fff;border:none;border-radius:4px;padding:6px 16px;cursor:pointer;"
        "font-size:13px;font-weight:600;\">&#128190; Save</button>'\n"
        "      +'<button id=\"_id_can\" style=\"background:#fff;color:#5c6bc0;"
        "border:1px solid #c5cae9;border-radius:4px;padding:6px 12px;"
        "cursor:pointer;font-size:13px;\">Cancel</button>';\n"
        "    document.body.prepend(b);\n"
        "    document.getElementById('_id_sav').addEventListener('click',_doSave);\n"
        "    document.getElementById('_id_can').addEventListener('click',_doCancel);\n"
        "  }\n"
        "  document.getElementById('_id_bar_t').textContent=title;\n"
        "  b.style.display='flex';\n"
        "}\n"
        "function _hideBar(){var b=_bar();if(b)b.style.display='none';}\n"

        # ── inline editor ────────────────────────────────────────────────
        "function _showEditor(sid,content){\n"
        "  if(!_hel)return;\n"
        "  var ed=document.getElementById('_id_ed');if(ed)ed.remove();\n"
        # hide original content between this heading and the next
        "  var el=_hel.nextElementSibling;\n"
        "  _orig=[];\n"
        "  while(el&&!/^H[1-4]$/.test(el.tagName)){\n"
        "    _orig.push({el:el,d:el.style.display});\n"
        "    el.style.display='none';\n"
        "    el=el.nextElementSibling;\n"
        "  }\n"
        # insert a contenteditable div with the Markdown content
        "  ed=document.createElement('div');\n"
        "  ed.id='_id_ed';\n"
        "  ed.contentEditable='true';\n"
        "  ed.style.cssText='border:2px solid #5c6bc0;border-radius:6px;padding:16px 20px;"
        "min-height:120px;outline:none;font-family:Consolas,\"Courier New\",monospace;"
        "font-size:13px;line-height:1.75;background:#f8f9ff;color:#1a1a2e;"
        "white-space:pre-wrap;word-wrap:break-word;margin:10px 0 16px 0;';\n"
        "  ed.textContent=content;\n"
        "  _hel.insertAdjacentElement('afterend',ed);\n"
        "  ed.focus();\n"
        "  var r=document.createRange(),s=window.getSelection();\n"
        "  r.selectNodeContents(ed);r.collapse(false);\n"
        "  s.removeAllRanges();s.addRange(r);\n"
        "}\n"

        # ── save / cancel ────────────────────────────────────────────────
        "function _doSave(){\n"
        "  var ed=document.getElementById('_id_ed');\n"
        "  if(!ed||!_sid)return;\n"
        "  var btn=document.getElementById('_id_sav');\n"
        "  btn.textContent='Saving…';btn.disabled=true;\n"
        "  window.parent.postMessage({type:'intellidraft_save_section',"
        "section_id:_sid,content:ed.innerText},'*');\n"
        "}\n"
        "function _doCancel(){\n"
        "  _orig.forEach(function(o){o.el.style.display=o.d;});\n"
        "  _orig=[];\n"
        "  var ed=document.getElementById('_id_ed');if(ed)ed.remove();\n"
        "  _hideBar();_sid=null;_hel=null;\n"
        "}\n"

        # ── listen for messages from parent ──────────────────────────────
        "window.addEventListener('message',function(e){\n"
        "  if(!e.data)return;\n"
        "  if(e.data.type==='intellidraft_section_content')_showEditor(e.data.section_id,e.data.content);\n"
        "  if(e.data.type==='intellidraft_save_complete')_doCancel();\n"
        "  if(e.data.type==='intellidraft_save_error'){\n"
        "    var btn=document.getElementById('_id_sav');\n"
        "    if(btn){btn.textContent='&#128190; Save';btn.disabled=false;}\n"
        "    alert('Save failed: '+(e.data.error||'unknown error'));\n"
        "  }\n"
        "});\n"

        # ── attach hover + click to headings ─────────────────────────────
        "function _attach(){\n"
        "  document.querySelectorAll('h1,h2,h3,h4').forEach(function(el){\n"
        "    var title=el.textContent.trim();\n"
        "    var secId=SM[title];\n"
        "    if(!secId)return;\n"
        "    el.style.cursor='pointer';\n"
        "    el.setAttribute('title','Click to edit this section');\n"
        "    el.style.transition='background .15s,border-left .15s';\n"
        "    el.style.paddingLeft='6px';\n"
        "    el.addEventListener('mouseenter',function(){\n"
        "      if(_sid!==secId){this.style.background='rgba(92,107,192,.1)';"
        "this.style.borderLeft='3px solid #5c6bc0';this.style.borderRadius='3px';}\n"
        "    });\n"
        "    el.addEventListener('mouseleave',function(){\n"
        "      if(_sid!==secId){this.style.background='';this.style.borderLeft='';}\n"
        "    });\n"
        "    el.addEventListener('click',function(){\n"
        "      if(_sid===secId)return;\n"   # already editing this section
        "      if(_sid)_doCancel();\n"       # cancel any previous edit
        "      _sid=secId;_hel=el;\n"
        "      _showBar(title);\n"
        "      window.parent.postMessage({type:'intellidraft_section_click',"
        "section_id:secId,title:title},'*');\n"
        "    });\n"
        "  });\n"
        "}\n"
        "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_attach);\n"
        "else _attach();\n"
        "})();\n"
        "</script>\n"
    )

    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script
