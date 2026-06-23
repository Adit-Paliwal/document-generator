"""
preview_service.py
==================
Production-grade LibreOffice document preview for IntelliDraft.

How it works
------------
1. Client calls GET /api/generate/{job_id}/preview/html
2. Service checks Redis for a cached HTML string (key: preview:{job_id}:{version_hash})
3. Cache hit  → return HTML immediately (< 5 ms)
4. Cache miss → submit a Celery task to the "preview" queue → return 202 + task_id
5. Celery worker picks up the task, calls LibreOffice headless to convert DOCX→HTML,
   stores result in Redis, returns HTML via Celery result backend
6. Client polls GET /api/generate/{job_id}/preview/status?task_id=...
7. When status==ready the client renders the HTML in an <iframe>

Parallelism
-----------
Each Celery task gets a unique -env:UserInstallation directory so multiple
LibreOffice processes can run simultaneously without profile-lock conflicts.
Set --concurrency=N on the worker to control parallelism (default 4).

Modes
-----
CELERY_ENABLED=true  — full async path with Redis + Celery (production)
CELERY_ENABLED=false — synchronous conversion in the Flask request (local dev,
                       no Redis or worker needed, but blocks the request thread)

Cache invalidation
------------------
Any PATCH to a section calls invalidate_preview_cache(job_id), which deletes
all preview:{job_id}:* keys from Redis so the next preview request triggers
a fresh conversion.
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
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

CELERY_ENABLED = os.environ.get("CELERY_ENABLED", "false").lower() == "true"
REDIS_URL      = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL      = int(os.environ.get("PREVIEW_CACHE_TTL", "3600"))   # seconds
LO_TIMEOUT     = int(os.environ.get("LO_CONVERT_TIMEOUT", "90"))    # seconds


# ─────────────────────────────────────────────────────────────────────────────
# Public API — called from Flask/Azure Function routes
# ─────────────────────────────────────────────────────────────────────────────

def get_or_submit_preview(job_id: str) -> dict:
    """
    Returns one of:
      {"status": "ready",   "html": "<html>…", "cached": bool}
      {"status": "pending", "task_id": "…",    "poll_url": "…"}
      {"status": "error",   "error": "…"}
    """
    if not CELERY_ENABLED:
        return _sync_preview(job_id)

    # Fast path — check Redis cache
    cached_html = _cache_get(job_id)
    if cached_html:
        html = _inject_section_handlers(cached_html, job_id)
        return {"status": "ready", "html": html, "cached": True}

    # Submit async Celery task
    try:
        from generation.preview_tasks import convert_docx_task
        task = convert_docx_task.apply_async(args=[job_id], queue="preview")
        return {
            "status":   "pending",
            "task_id":  task.id,
            "poll_url": f"/api/generate/{job_id}/preview/status?task_id={task.id}",
        }
    except Exception as e:
        logger.exception("[preview] Task submission failed for job %s", job_id)
        return {"status": "error", "error": str(e)}


def poll_preview_status(job_id: str, task_id: str) -> dict:
    """
    Check Celery task state.  Returns same shape as get_or_submit_preview.
    The cache is checked first — if another caller already completed the
    conversion its result is served immediately.
    """
    # Check Redis first (avoids Celery round-trip if result already cached)
    cached_html = _cache_get(job_id)
    if cached_html:
        return {"status": "ready", "html": cached_html, "cached": True}

    try:
        from celery.result import AsyncResult
        from celery_app import celery_app
        result = AsyncResult(task_id, app=celery_app)

        if result.state == "SUCCESS":
            html = _inject_section_handlers(result.result, job_id)
            return {"status": "ready", "html": html, "cached": False}
        elif result.state == "FAILURE":
            err = str(result.result) if result.result else "Unknown conversion error"
            return {"status": "error", "error": err}
        else:
            # PENDING | STARTED | RETRY
            return {"status": "pending", "state": result.state}

    except Exception as e:
        logger.exception("[preview] Status poll failed for task %s", task_id)
        return {"status": "error", "error": str(e)}


def invalidate_preview_cache(job_id: str) -> None:
    """
    Delete all cached previews for this job from Redis.
    Call this after any section is patched (manual edit) or regenerated.
    Safe to call even when CELERY_ENABLED=false (no-op).
    """
    if not CELERY_ENABLED:
        return
    try:
        r = _redis_client()
        pattern = f"preview:{job_id}:*"
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            if keys:
                r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        if deleted:
            logger.debug("[preview] Invalidated %d cache key(s) for job %s", deleted, job_id)
    except Exception as e:
        logger.warning("[preview] Cache invalidation failed (non-fatal): %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Core conversion — called by the Celery task and the sync fallback
# ─────────────────────────────────────────────────────────────────────────────

def convert_job_to_html(job_id: str) -> str:
    """
    Export job sections as DOCX and convert to self-contained HTML via LibreOffice.
    Each call uses a unique LO user-profile directory so multiple conversions
    can run in parallel without lock conflicts.

    Returns the HTML string (CSS and small images are inlined).
    Raises on any failure — the Celery task handles retries.
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
    """Local-dev fallback: convert synchronously inside the Flask request."""
    try:
        html = convert_job_to_html(job_id)
        html = _inject_section_handlers(html, job_id)
        return {"status": "ready", "html": html, "cached": False}
    except EnvironmentError as e:
        # LibreOffice not installed — surface a clear message
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("[preview] Sync conversion failed for job %s", job_id)
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
        "LibreOffice (soffice) not found. "
        "Install it and add it to your PATH. "
        "See PREVIEW_SETUP.md for step-by-step instructions."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Redis cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _redis_client():
    import redis as _redis
    return _redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)


def _version_hash(job_id: str) -> str:
    """
    Cached MD5 of {section_id}:{current_version} per section.
    Lookup is O(1) per section (no MD5 recalc).
    Cache is invalidated when current_version changes (PATCH section).
    """
    from generation.db import GenerationJob, get_session
    try:
        with get_session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                return "nodata"
            # Use pre-computed version_hash from each section (auto-migrated on startup)
            ids = sorted(
                sec.version_hash or f"{sec.section_id}:{sec.current_version}"
                for sec in job.sections if sec.version_hash
            )
            if not ids:
                # Fallback: compute on first run (before version_hash column is populated)
                ids = sorted(
                    f"{sec.section_id}:{sec.current_version}"
                    for sec in job.sections
                )
            return hashlib.md5("|".join(ids).encode()).hexdigest()[:16]
    except Exception:
        return uuid.uuid4().hex[:16]


def _cache_key(job_id: str) -> str:
    return f"preview:{job_id}:{_version_hash(job_id)}"


def _cache_get(job_id: str) -> str | None:
    try:
        return _redis_client().get(_cache_key(job_id))
    except Exception as e:
        logger.warning("[preview] Redis GET failed (non-fatal): %s", e)
        return None


def _cache_set(job_id: str, html: str) -> None:
    try:
        _redis_client().setex(_cache_key(job_id), CACHE_TTL, html)
        logger.debug("[preview] Cached HTML for job %s (TTL %ds)", job_id, CACHE_TTL)
    except Exception as e:
        logger.warning("[preview] Redis SET failed (non-fatal): %s", e)


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
