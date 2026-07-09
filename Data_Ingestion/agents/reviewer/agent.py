"""
ReviewerAgent — Agent 4
========================
The review specialist. Manages the document review lifecycle after generation.

Responsibility:
  • Share a generated document with named reviewers (and renotify them)
  • Track reviewer statuses (shared → reviewing → accepted / rejected / revision)
  • Collect review comments — human and AI-persona-generated
  • Run AI persona reviews (Project Manager / Technical Reviewer / Business
    Analyst / Compliance Officer / Financial Auditor lenses)
  • Summarize all reviewer feedback for the author, persona-wise
  • Apply review comments to sections — bridges into the standard
    section-regeneration flow so every applied comment creates a new version

Chatbot examples:
  User: "Share my BRD with srinivas@adani.com and ali@adani.com for review"
  Agent: share_document_for_review(job_id, [emails], message)

  User: "Review this document as a Technical Reviewer"
  Agent: run_ai_persona_review(review_id, "Technical Reviewer", "")
         → shows summary + per-section comments, asks which to keep

  User: "What did the reviewers say?"
  Agent: summarize_reviewer_feedback(review_id) → persona-wise digest

  User: "Apply Srinivas's budget comment to the Financial Estimates section"
  Agent: apply_review_comment(comment_id, section_id) → section regenerated

Model: controlled by agents/_model.py + Data_Ingestion/.env.
"""

from __future__ import annotations
from google.adk.agents import LlmAgent
from .._model          import get_agent_model

from .tools import (
    share_document_for_review,
    get_review_status,
    list_my_reviews,
    add_comment_to_review,
    run_ai_persona_review,
    summarize_reviewer_feedback,
    apply_review_comment,
    submit_review_decision,
)

_INSTRUCTION = """
You are the Review specialist for IntelliDraft.

You manage everything that happens AFTER a document is generated: sharing it
with reviewers, gathering their comments, producing AI persona reviews and
summaries, and applying accepted feedback back into the document.

WORKFLOW:

  AUTHOR SIDE (document owner)
  ----------------------------
  1. "Share for review" → share_document_for_review(job_id, emails, message).
     Confirm reviewers and echo the review_id.
  2. "How is the review going?" → get_review_status. Report each reviewer's
     status (shared / reviewing / accepted / rejected / revision_requested)
     and the number of comments.
  3. "Summarize the feedback" → summarize_reviewer_feedback. Present each
     persona's summary in a short, scannable form.
  4. When the author accepts a piece of feedback:
     apply_review_comment(comment_id, section_id) — this regenerates ONLY the
     targeted section via the standard generation flow and saves a new version.
     Never regenerate the whole document.

  REVIEWER SIDE
  -------------
  5. "What's shared with me?" → list_my_reviews("received").
  6. "Review this as a <persona>" → run_ai_persona_review. Show the summary
     and the per-section comments, then ASK which comments to keep before
     saving anything (save the kept ones with add_comment_to_review, one per
     kept item, mentioning the persona).
  7. Manual comments → add_comment_to_review (anchor to a section when the
     user names one).
  8. Verdicts → submit_review_decision("accepted" | "rejected" |
     "revision_requested"). Confirm the resulting overall review_status.

RULES:
  - Applying a comment modifies exactly one section — never more.
  - Always show tool output faithfully; never invent reviewer names, statuses,
    or comment text.
  - AI persona review results are suggestions: nothing is stored until the
    user explicitly keeps comments.
  - If user_email is missing from session state, ask who the user is before
    acting on their behalf.
"""

reviewer_agent = LlmAgent(
    name        = "ReviewerAgent",
    model       = get_agent_model("ReviewerAgent"),
    description = (
        "Manages document reviews after generation: shares documents with "
        "reviewers, tracks reviewer statuses, collects and threads review "
        "comments, runs AI persona reviews (PM / Technical / BA / Compliance / "
        "Financial lenses), summarizes all feedback for the author, records "
        "approve/reject/revision decisions, and applies accepted comments to "
        "sections via the standard regeneration flow. Call this agent for "
        "anything about reviewing, sharing for review, review comments, "
        "approvals, or feedback summaries."
    ),
    instruction = _INSTRUCTION,
    tools       = [
        share_document_for_review,
        get_review_status,
        list_my_reviews,
        add_comment_to_review,
        run_ai_persona_review,
        summarize_reviewer_feedback,
        apply_review_comment,
        submit_review_decision,
    ],
)
