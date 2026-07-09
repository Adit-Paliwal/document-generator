"""
Review Service — the Review Agent
==================================
Implements the Figma "Review" flow end-to-end:

  Author side (sent):
    share_for_review()        → share a generated document with named reviewers
    list_sent()               → my shared documents + per-reviewer statuses
    summarize_for_author()    → AI persona-wise summaries of all reviewer feedback
    renotify()                → nudge reviewers (stub notification, logged)
    apply_comment_to_section()→ BRIDGE: turn a review comment into the existing
                                add_comment + regenerate_section flow (new version)

  Reviewer side (received):
    list_received()           → documents shared with me
    get_review_workspace()    → document + reviewers + threaded comments + summaries
    ai_persona_review()       → AI summary + per-section comments for a persona
    keep_ai_comments()        → persist the AI comments the reviewer chose to keep
    respond()                 → approve / reject / request_revision
                                (rolls up GenerationJob.review_status)

  Shared:
    comment CRUD + replies + resolve, personas CRUD, users CRUD.

Identity: callers pass author/reviewer identity dicts {email, name} taken from
the X-User-Email / X-User-Name headers (Entra ID SSO happens on the frontend).

All LLM calls go through llm_provider.call_with_fallback — same provider chain
as the generator (Gemini primary, Azure GPT fallback).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from generation.db import (
    DEFAULT_PERSONAS,
    GenerationJob,
    Persona,
    ReviewAssignment,
    ReviewComment,
    ReviewRequest,
    ReviewSummary,
    Section,
    User,
    get_session,
)

logger = logging.getLogger(__name__)

# Review-status rollup order (worst wins where it matters)
_VALID_RESPONSES = {"accepted", "rejected", "revision_requested"}


# ─────────────────────────────────────────────────────────────────────────────
# Personas
# ─────────────────────────────────────────────────────────────────────────────

_personas_seeded = False


def ensure_personas_seeded() -> None:
    """Seed the 5 default system personas once (idempotent)."""
    global _personas_seeded
    if _personas_seeded:
        return
    with get_session() as s:
        existing = {p.name for p in s.query(Persona).filter(Persona.is_system == True).all()}  # noqa: E712
        for spec in DEFAULT_PERSONAS:
            if spec["name"] not in existing:
                s.add(Persona(
                    persona_id  = str(uuid.uuid4()),
                    name        = spec["name"],
                    description = spec["description"],
                    is_system   = True,
                ))
        s.commit()
    _personas_seeded = True


def list_personas(owner_email: Optional[str] = None) -> list[dict]:
    """System personas + the caller's own custom personas."""
    ensure_personas_seeded()
    with get_session() as s:
        q = s.query(Persona)
        if owner_email:
            from sqlalchemy import or_
            q = q.filter(or_(Persona.is_system == True, Persona.owner_email == owner_email))  # noqa: E712
        else:
            q = q.filter(Persona.is_system == True)  # noqa: E712
        return [p.to_dict() for p in q.order_by(Persona.is_system.desc(), Persona.name).all()]


def create_persona(name: str, description: str, owner_email: Optional[str]) -> dict:
    ensure_personas_seeded()
    with get_session() as s:
        p = Persona(
            persona_id  = str(uuid.uuid4()),
            name        = name.strip(),
            description = (description or "").strip(),
            is_system   = False,
            owner_email = owner_email,
        )
        s.add(p)
        s.commit()
        return p.to_dict()


def update_persona(persona_id: str, name: Optional[str], description: Optional[str]) -> dict:
    with get_session() as s:
        p = s.get(Persona, persona_id)
        if not p:
            raise FileNotFoundError(f"Persona '{persona_id}' not found")
        if p.is_system:
            raise PermissionError("System personas cannot be edited")
        if name:
            p.name = name.strip()
        if description is not None:
            p.description = description.strip()
        s.commit()
        return p.to_dict()


def delete_persona(persona_id: str) -> None:
    with get_session() as s:
        p = s.get(Persona, persona_id)
        if not p:
            raise FileNotFoundError(f"Persona '{persona_id}' not found")
        if p.is_system:
            raise PermissionError("System personas cannot be deleted")
        s.delete(p)
        s.commit()


def _persona_description(name: str) -> str:
    with get_session() as s:
        p = s.query(Persona).filter(Persona.name == name).first()
        if p and p.description:
            return p.description
    return "Provide a balanced professional document review."


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

def upsert_user(email: str, name: str, role: str = "Contributor") -> dict:
    """Create or update a user record keyed by email."""
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if u:
            if name:
                u.name = name
            if role:
                u.role = role
        else:
            u = User(user_id=str(uuid.uuid4()), email=email, name=name or email, role=role)
            s.add(u)
        s.commit()
        return u.to_dict()


def list_users() -> list[dict]:
    with get_session() as s:
        return [u.to_dict() for u in s.query(User).order_by(User.name).all()]


def delete_user(user_id: str) -> None:
    with get_session() as s:
        u = s.get(User, user_id)
        if not u:
            raise FileNotFoundError(f"User '{user_id}' not found")
        s.delete(u)
        s.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Share for review (author)
# ─────────────────────────────────────────────────────────────────────────────

def share_for_review(
    job_id: str,
    requested_by: dict,                 # {email, name}
    reviewers: list[dict],              # [{email, name?, role?}, ...]
    message: Optional[str] = None,
) -> dict:
    """Create a ReviewRequest + one assignment per reviewer. Marks the document under_review."""
    if not reviewers:
        raise ValueError("At least one reviewer is required")
    if not requested_by.get("email"):
        raise ValueError("requested_by.email is required")

    with get_session() as s:
        job = s.get(GenerationJob, job_id)
        if not job:
            raise FileNotFoundError(f"Job '{job_id}' not found")

        # Find the linked project (for dashboard grouping)
        from generation.db import Project as _P
        proj = s.query(_P).filter(_P.job_id == job_id).first()

        review = ReviewRequest(
            review_id          = str(uuid.uuid4()),
            job_id             = job_id,
            project_id         = proj.project_id if proj else None,
            document_type      = job.document_type,
            requested_by_email = requested_by["email"],
            requested_by_name  = requested_by.get("name"),
            message            = message,
            status             = "open",
        )
        s.add(review)

        seen: set[str] = set()
        for r in reviewers:
            email = (r.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            s.add(ReviewAssignment(
                assignment_id  = str(uuid.uuid4()),
                review_id      = review.review_id,
                reviewer_email = email,
                reviewer_name  = r.get("name"),
                reviewer_role  = r.get("role"),
                status         = "shared",
            ))
            # Auto-register reviewer as a user so the Admin Panel sees them
            if not s.query(User).filter(User.email == email).first():
                s.add(User(user_id=str(uuid.uuid4()), email=email,
                           name=r.get("name") or email, role="Contributor"))

        job.review_status = "under_review"
        s.commit()
        result = review.to_dict()

    logger.info("[review] Shared job %s for review with %d reviewer(s)", job_id, len(seen))
    return result


def renotify(review_id: str) -> dict:
    """Stub notification: stamps last_renotified_at and logs (no email infra yet)."""
    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")
        now = datetime.utcnow()
        pending = 0
        for a in review.assignments:
            if a.status in ("shared", "reviewing"):
                a.last_renotified_at = now
                pending += 1
        s.commit()
    logger.info("[review] Renotified %d pending reviewer(s) on review %s", pending, review_id)
    return {"review_id": review_id, "renotified": pending, "at": now.isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Dashboards
# ─────────────────────────────────────────────────────────────────────────────

def list_sent(email: str) -> list[dict]:
    """Reviews the caller has requested, newest first."""
    with get_session() as s:
        rows = (
            s.query(ReviewRequest)
            .filter(ReviewRequest.requested_by_email == email)
            .order_by(ReviewRequest.created_at.desc())
            .all()
        )
        out = []
        for r in rows:
            d = r.to_dict()
            d["days_since_shared"] = (datetime.utcnow() - r.created_at).days if r.created_at else 0
            d["project_name"] = _project_name(s, r.project_id)
            out.append(d)
        return out


def list_received(email: str) -> list[dict]:
    """Reviews where the caller is a reviewer, newest first."""
    email = (email or "").strip().lower()
    with get_session() as s:
        rows = (
            s.query(ReviewAssignment)
            .filter(ReviewAssignment.reviewer_email == email)
            .all()
        )
        out = []
        for a in rows:
            r = a.review
            if not r or r.status == "cancelled":
                continue
            out.append({
                "review_id":      r.review_id,
                "job_id":         r.job_id,
                "project_id":     r.project_id,
                "project_name":   _project_name(s, r.project_id),
                "document_type":  r.document_type,
                "from":           {"email": r.requested_by_email, "name": r.requested_by_name},
                "message":        r.message,
                "my_status":      a.status,
                "shared_on":      r.created_at.isoformat() if r.created_at else None,
                "days_since_shared": (datetime.utcnow() - r.created_at).days if r.created_at else 0,
            })
        out.sort(key=lambda d: d["shared_on"] or "", reverse=True)
        return out


def _project_name(s, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    from generation.db import Project as _P
    p = s.get(_P, project_id)
    return p.project_name if p else None


# ─────────────────────────────────────────────────────────────────────────────
# Review workspace (open one review)
# ─────────────────────────────────────────────────────────────────────────────

def get_review_workspace(review_id: str, viewer_email: Optional[str] = None) -> dict:
    """
    Everything the review screen needs: review meta, reviewers, threaded
    comments, cached AI summaries, and the document sections (id/title/content)
    so comments can be anchored per section.

    Side effect: if the viewer is a reviewer whose status is 'shared', it flips
    to 'reviewing' (matches the Figma status progression).
    """
    viewer_email = (viewer_email or "").strip().lower()
    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")

        # Status flip: shared → reviewing on first open by that reviewer
        if viewer_email:
            for a in review.assignments:
                if a.reviewer_email == viewer_email and a.status == "shared":
                    a.status = "reviewing"

        job = s.get(GenerationJob, review.job_id)
        sections = []
        if job:
            for sec in sorted(job.sections, key=lambda x: x.order_index):
                latest = max(sec.versions, key=lambda v: v.version_number) if sec.versions else None
                sections.append({
                    "section_id":    sec.section_id,
                    "section_title": sec.section_title,
                    "order":         sec.order_index,
                    "status":        sec.status,
                    "content":       latest.content if latest else "",
                    "version":       latest.version_number if latest else 0,
                })

        # Threaded comments: top-level with nested replies
        top    = [c for c in review.comments if not c.parent_id]
        by_parent: dict[str, list] = {}
        for c in review.comments:
            if c.parent_id:
                by_parent.setdefault(c.parent_id, []).append(c)

        def thread(c: ReviewComment) -> dict:
            d = c.to_dict()
            d["replies"] = [thread(r) for r in by_parent.get(c.comment_id, [])]
            return d

        summaries = (
            s.query(ReviewSummary)
            .filter(ReviewSummary.review_id == review_id)
            .order_by(ReviewSummary.created_at.desc())
            .all()
        )
        # keep only the latest summary per persona
        latest_by_persona: dict[str, ReviewSummary] = {}
        for sm in summaries:
            latest_by_persona.setdefault(sm.persona, sm)

        d = review.to_dict()
        d["review_status"]  = job.review_status if job else None
        d["project_name"]   = _project_name(s, review.project_id)
        d["sections"]       = sections
        d["comments"]       = [thread(c) for c in top]
        d["ai_summaries"]   = [sm.to_dict() for sm in latest_by_persona.values()]
        s.commit()   # persist any shared→reviewing flip
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Comments (add / edit / delete / resolve / reply)
# ─────────────────────────────────────────────────────────────────────────────

def add_review_comment(
    review_id: str,
    author: dict,                       # {email, name}
    text: str,
    section_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    source: str = "user",
    persona: Optional[str] = None,
) -> dict:
    if not (text or "").strip():
        raise ValueError("Comment text is required")
    if not author.get("email"):
        raise ValueError("author.email is required")

    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")
        if parent_id:
            parent = s.get(ReviewComment, parent_id)
            if not parent or parent.review_id != review_id:
                raise ValueError("parent_id does not belong to this review")

        section_title = None
        if section_id:
            sec = s.get(Section, section_id)
            section_title = sec.section_title if sec else None

        c = ReviewComment(
            comment_id   = str(uuid.uuid4()),
            review_id    = review_id,
            section_id   = section_id,
            section_title = section_title,
            parent_id    = parent_id,
            author_email = author["email"].strip().lower(),
            author_name  = author.get("name"),
            source       = source if source in ("user", "ai") else "user",
            persona      = persona,
            text         = text.strip(),
        )
        s.add(c)
        s.commit()
        return c.to_dict()


def update_review_comment(comment_id: str, editor_email: str, text: str) -> dict:
    with get_session() as s:
        c = s.get(ReviewComment, comment_id)
        if not c:
            raise FileNotFoundError(f"Comment '{comment_id}' not found")
        if c.author_email != (editor_email or "").strip().lower():
            raise PermissionError("Only the comment author can edit it")
        c.text = text.strip()
        c.updated_at = datetime.utcnow()
        s.commit()
        return c.to_dict()


def delete_review_comment(comment_id: str, editor_email: str) -> None:
    with get_session() as s:
        c = s.get(ReviewComment, comment_id)
        if not c:
            raise FileNotFoundError(f"Comment '{comment_id}' not found")
        if c.author_email != (editor_email or "").strip().lower():
            raise PermissionError("Only the comment author can delete it")
        # delete replies too
        s.query(ReviewComment).filter(ReviewComment.parent_id == comment_id).delete()
        s.delete(c)
        s.commit()


def resolve_review_comment(comment_id: str, resolved: bool = True) -> dict:
    with get_session() as s:
        c = s.get(ReviewComment, comment_id)
        if not c:
            raise FileNotFoundError(f"Comment '{comment_id}' not found")
        c.status = "resolved" if resolved else "open"
        c.updated_at = datetime.utcnow()
        s.commit()
        return c.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer response → document review_status rollup
# ─────────────────────────────────────────────────────────────────────────────

def respond(review_id: str, reviewer_email: str, action: str) -> dict:
    """
    Reviewer verdict: accepted | rejected | revision_requested.
    Rolls up GenerationJob.review_status:
      any rejected            → rejected
      else any revision req.  → revision_requested
      else all accepted       → approved  (review marked completed)
      else                    → under_review
    """
    action = (action or "").strip().lower()
    if action not in _VALID_RESPONSES:
        raise ValueError(f"action must be one of {sorted(_VALID_RESPONSES)}")
    reviewer_email = (reviewer_email or "").strip().lower()

    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")

        mine = next((a for a in review.assignments if a.reviewer_email == reviewer_email), None)
        if not mine:
            raise PermissionError(f"'{reviewer_email}' is not a reviewer on this review")
        mine.status       = action
        mine.responded_at = datetime.utcnow()

        statuses = {a.status for a in review.assignments}
        if "rejected" in statuses:
            rollup = "rejected"
        elif "revision_requested" in statuses:
            rollup = "revision_requested"
        elif statuses == {"accepted"}:
            rollup = "approved"
            review.status = "completed"
        else:
            rollup = "under_review"

        job = s.get(GenerationJob, review.job_id)
        if job:
            job.review_status = rollup
        s.commit()

        return {
            "review_id":     review_id,
            "reviewer":      reviewer_email,
            "action":        action,
            "review_status": rollup,
            "reviewers":     [a.to_dict() for a in review.assignments],
        }


# ─────────────────────────────────────────────────────────────────────────────
# BRIDGE — apply a review comment to a section via the existing generation flow
# ─────────────────────────────────────────────────────────────────────────────

def apply_comment_to_section(comment_id: str, section_id: Optional[str] = None) -> dict:
    """
    Convert a review comment into the existing section-modification flow:
      SectionComment (edit_request) → regenerate_section → new SectionVersion.

    Uses the comment's own section anchor unless an explicit section_id is
    passed (for un-anchored, document-level comments).
    Marks the review comment resolved and links the created SectionComment.
    """
    from generation.generation_service import add_comment, regenerate_section

    with get_session() as s:
        c = s.get(ReviewComment, comment_id)
        if not c:
            raise FileNotFoundError(f"Comment '{comment_id}' not found")
        target_section = section_id or c.section_id
        if not target_section:
            raise ValueError(
                "This comment is not anchored to a section — pass section_id "
                "to choose which section it should be applied to."
            )
        text     = c.text
        persona  = c.persona
        author   = c.author_name or c.author_email

    instruction = f"[Review feedback from {author}{' as ' + persona if persona else ''}]: {text}"
    sec_comment = add_comment(target_section, instruction, "edit_request")
    new_version = regenerate_section(target_section, sec_comment["comment_id"])

    with get_session() as s:
        c = s.get(ReviewComment, comment_id)
        if c:
            c.status = "resolved"
            c.applied_section_comment_id = sec_comment["comment_id"]
            c.updated_at = datetime.utcnow()
            s.commit()

    logger.info("[review] Applied comment %s to section %s → v%s",
                comment_id, target_section, new_version.get("version_number"))
    return {
        "comment_id":          comment_id,
        "section_id":          target_section,
        "section_comment_id":  sec_comment["comment_id"],
        "new_version":         new_version,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AI — persona review (reviewer side)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_SECTION_CHARS = 1800   # per-section cap in the AI prompt
_MAX_SECTIONS      = 30


def ai_persona_review(review_id: str, persona: str, instructions: str = "") -> dict:
    """
    Generate a persona-lens review of the document:
      { persona, summary, section_comments: [{section_id, section_title, comment}] }

    Nothing is persisted — the reviewer chooses which comments to keep via
    keep_ai_comments() (the Figma "Keep Selected" action).
    """
    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")
        job = s.get(GenerationJob, review.job_id)
        if not job:
            raise FileNotFoundError(f"Job '{review.job_id}' not found")
        doc_type = job.document_type
        sections = []
        for sec in sorted(job.sections, key=lambda x: x.order_index)[:_MAX_SECTIONS]:
            latest = max(sec.versions, key=lambda v: v.version_number) if sec.versions else None
            if latest and latest.content:
                sections.append({
                    "section_id":    sec.section_id,
                    "section_title": sec.section_title,
                    "content":       latest.content[:_MAX_SECTION_CHARS],
                })

    if not sections:
        raise ValueError("Document has no generated content to review yet")

    persona_desc = _persona_description(persona)
    section_block = "\n\n".join(
        f"[SECTION id={sec['section_id']}]\n## {sec['section_title']}\n{sec['content']}"
        for sec in sections
    )
    extra = f"\nADDITIONAL REVIEWER INSTRUCTIONS: {instructions.strip()}\n" if (instructions or "").strip() else ""

    prompt = f"""You are an expert document reviewer acting as a **{persona}** ({persona_desc}).
Review the following {doc_type} strictly from that persona's point of view.{extra}
DOCUMENT SECTIONS:
{section_block}

Return ONLY valid JSON (no markdown fences, no preamble) in exactly this shape:
{{
  "summary": "<4-6 sentence overall assessment from the {persona} perspective — strengths, gaps, and the most important recommendation>",
  "section_comments": [
    {{"section_id": "<id from the [SECTION id=...] tag>", "section_title": "<title>", "comment": "<one specific, actionable review comment for this section>"}}
  ]
}}
Rules: comment on the 3-6 sections MOST relevant to a {persona}; be concrete and reference actual content; do not praise without substance."""

    from llm_provider import call_with_fallback
    raw, model_id = call_with_fallback(
        messages   = [{"role": "user", "content": prompt}],
        max_tokens = 2500,
        timeout    = 120,
        log_prefix = f"[ReviewAgent:{persona}]",
    )

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict) or "summary" not in parsed:
        raise RuntimeError("AI review returned an unexpected format — please retry")

    valid_ids = {sec["section_id"] for sec in sections}
    title_by_id = {sec["section_id"]: sec["section_title"] for sec in sections}
    comments = []
    for item in parsed.get("section_comments", []) or []:
        if not isinstance(item, dict):
            continue
        sid = item.get("section_id")
        if sid not in valid_ids or not (item.get("comment") or "").strip():
            continue
        comments.append({
            "section_id":    sid,
            "section_title": item.get("section_title") or title_by_id.get(sid, ""),
            "comment":       item["comment"].strip(),
        })

    return {
        "review_id":        review_id,
        "persona":          persona,
        "summary":          str(parsed.get("summary", "")).strip(),
        "section_comments": comments,
        "model":            model_id,
    }


def keep_ai_comments(
    review_id: str,
    author: dict,                       # the reviewer keeping them {email, name}
    persona: str,
    comments: list[dict],               # [{section_id?, section_title?, comment|text}]
) -> list[dict]:
    """Persist the AI comments the reviewer selected ("Keep Selected")."""
    kept = []
    for item in comments or []:
        text = (item.get("comment") or item.get("text") or "").strip()
        if not text:
            continue
        kept.append(add_review_comment(
            review_id  = review_id,
            author     = author,
            text       = text,
            section_id = item.get("section_id"),
            source     = "ai",
            persona    = persona,
        ))
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# AI — summarize reviewer feedback for the author (sent side)
# ─────────────────────────────────────────────────────────────────────────────

def summarize_for_author(review_id: str, personas: Optional[list[str]] = None) -> list[dict]:
    """
    Persona-wise AI summaries of ALL reviewer comments (the author's carousel).
    Cached in review_summaries (latest per persona wins).
    """
    ensure_personas_seeded()
    with get_session() as s:
        review = s.get(ReviewRequest, review_id)
        if not review:
            raise FileNotFoundError(f"Review '{review_id}' not found")
        doc_type = review.document_type or "document"
        comment_lines = []
        for c in review.comments:
            if c.parent_id:
                prefix = "  ↳ reply"
            else:
                prefix = c.section_title or "General"
            who = c.author_name or c.author_email
            tag = f" (AI:{c.persona})" if c.source == "ai" and c.persona else ""
            comment_lines.append(f"- [{prefix}] {who}{tag}: {c.text}")
    if not comment_lines:
        raise ValueError("No reviewer comments to summarize yet")

    if not personas:
        personas = [p["name"] for p in DEFAULT_PERSONAS[:3]]   # PM / Tech / BA by default

    feedback_block = "\n".join(comment_lines[:120])
    from llm_provider import call_with_fallback

    results = []
    for persona in personas:
        desc = _persona_description(persona)
        prompt = f"""You are a **{persona}** ({desc}).
Below is all reviewer feedback collected on a {doc_type}.

REVIEWER FEEDBACK:
{feedback_block}

Write a concise summary (3-5 sentences) of this feedback FROM THE {persona} PERSPECTIVE for the document's author:
what reviewers are saying that matters to a {persona}, the key risks/asks, and the single most important action.
Return plain text only — no headings, no bullets, no preamble."""
        try:
            text, model_id = call_with_fallback(
                messages   = [{"role": "user", "content": prompt}],
                max_tokens = 700,
                timeout    = 90,
                log_prefix = f"[ReviewAgent:summary:{persona}]",
            )
        except Exception as e:
            logger.warning("[review] Summary for persona '%s' failed: %s", persona, e)
            continue

        with get_session() as s:
            sm = ReviewSummary(
                summary_id   = str(uuid.uuid4()),
                review_id    = review_id,
                persona      = persona,
                summary_text = text.strip(),
                model        = model_id,
            )
            s.add(sm)
            s.commit()
            results.append(sm.to_dict())

    if not results:
        raise RuntimeError("All persona summaries failed — check LLM connectivity")
    return results


def get_summaries(review_id: str) -> list[dict]:
    """Latest cached summary per persona."""
    with get_session() as s:
        rows = (
            s.query(ReviewSummary)
            .filter(ReviewSummary.review_id == review_id)
            .order_by(ReviewSummary.created_at.desc())
            .all()
        )
        latest: dict[str, dict] = {}
        for sm in rows:
            latest.setdefault(sm.persona, sm.to_dict())
        return list(latest.values())


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(raw: str):
    """Parse the first JSON object out of an LLM response (tolerates fences)."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None
