"""
Reviewer Tools
===============
ADK tools for Agent 4 — ReviewerAgent.

All tools are thin wrappers over the review service — NO business logic here.
Everything delegates to generation.review_service (the Review Agent core).

Tools:
  share_document_for_review — share a generated document with named reviewers
  get_review_status         — reviewers + statuses + comment count for a review
  list_my_reviews           — reviews I sent / reviews shared with me
  add_comment_to_review     — add a (optionally section-anchored) review comment
  run_ai_persona_review     — AI summary + per-section comments for a persona
  summarize_reviewer_feedback — persona-wise AI summaries for the author
  apply_review_comment      — apply a review comment to a section (regenerates it)
  submit_review_decision    — approve / reject / request revision
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


async def share_document_for_review(
    job_id: str,
    reviewer_emails: list[str],
    message: str,
    tool_context: ToolContext,
) -> dict:
    """Share a generated document (job) with reviewers by email.

    Args:
        job_id:          The generation job whose document is being shared.
        reviewer_emails: Email addresses of the reviewers.
        message:         Optional note shown to reviewers ("" for none).

    Returns:
        The created review request with reviewer assignments.
    """
    from generation.review_service import share_for_review
    me = {
        "email": tool_context.state.get("user_email", "author@intellidraft.local"),
        "name":  tool_context.state.get("user_name", "Author"),
    }
    reviewers = [{"email": e} for e in reviewer_emails]
    result = share_for_review(job_id, me, reviewers, message or None)
    tool_context.state["review_id"] = result["review_id"]
    return result


async def get_review_status(review_id: str, tool_context: ToolContext) -> dict:
    """Get the full state of a review: reviewers with statuses, comments, summaries.

    Args:
        review_id: The review to inspect (falls back to session state if "").
    """
    from generation.review_service import get_review_workspace
    rid = review_id or tool_context.state.get("review_id", "")
    return get_review_workspace(rid)


async def list_my_reviews(direction: str, tool_context: ToolContext) -> dict:
    """List reviews for the current user.

    Args:
        direction: "sent" (reviews I requested) or "received" (shared with me).
    """
    from generation.review_service import list_sent, list_received
    email = tool_context.state.get("user_email", "")
    if direction == "received":
        return {"reviews": list_received(email)}
    return {"reviews": list_sent(email)}


async def add_comment_to_review(
    review_id: str,
    text: str,
    section_id: str,
    tool_context: ToolContext,
) -> dict:
    """Add a comment to a review, optionally anchored to a section.

    Args:
        review_id:  The review being commented on.
        text:       The comment text.
        section_id: Section to anchor to ("" for a document-level comment).
    """
    from generation.review_service import add_review_comment
    me = {
        "email": tool_context.state.get("user_email", "reviewer@intellidraft.local"),
        "name":  tool_context.state.get("user_name", "Reviewer"),
    }
    return add_review_comment(review_id, me, text, section_id=section_id or None)


async def run_ai_persona_review(
    review_id: str,
    persona: str,
    instructions: str,
    tool_context: ToolContext,
) -> dict:
    """Generate an AI review of the document through a persona lens.

    Args:
        review_id:    The review whose document should be analysed.
        persona:      e.g. "Project Manager", "Technical Reviewer", "Business Analyst".
        instructions: Extra reviewer instructions for the AI ("" for none).

    Returns:
        {summary, section_comments:[{section_id, section_title, comment}]}
        Nothing is persisted — show the comments and ask which to keep.
    """
    from generation.review_service import ai_persona_review
    return ai_persona_review(review_id, persona or "Project Manager", instructions or "")


async def summarize_reviewer_feedback(review_id: str, tool_context: ToolContext) -> dict:
    """Generate persona-wise AI summaries of all reviewer comments (for the author).

    Args:
        review_id: The review whose feedback should be summarized.
    """
    from generation.review_service import summarize_for_author
    return {"summaries": summarize_for_author(review_id)}


async def apply_review_comment(
    comment_id: str,
    section_id: str,
    tool_context: ToolContext,
) -> dict:
    """Apply a review comment to a document section — regenerates that section
    through the standard generation flow and creates a new version.

    Args:
        comment_id: The review comment to apply.
        section_id: Target section ("" to use the comment's own anchor).
    """
    from generation.review_service import apply_comment_to_section
    return apply_comment_to_section(comment_id, section_id or None)


async def submit_review_decision(
    review_id: str,
    action: str,
    tool_context: ToolContext,
) -> dict:
    """Submit the reviewer's verdict on a shared document.

    Args:
        review_id: The review being decided.
        action:    "accepted", "rejected", or "revision_requested".
    """
    from generation.review_service import respond
    email = tool_context.state.get("user_email", "")
    return respond(review_id, email, action)
