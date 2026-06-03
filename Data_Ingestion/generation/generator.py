"""
Section Generator
==================
Core LLM call for generating a single document section.

Uses the same LiteLLM / Azure GPT-5 configuration as the ADK agent.
Prompt strategy:
  - System prompt: role, document type, full document context, user inputs
  - User prompt: section-specific instructions + any edit comment
  - Previous sections (already generated) are appended to system prompt
    for coherence — the LLM knows what it already wrote

Supports:
  - First-time generation of a section
  - Re-generation with an edit comment (revision mode)
"""

from __future__ import annotations
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """You are a professional business analyst and technical writer.
You are generating the "{document_type}" document for the project described below.

## PROJECT INFORMATION
- **Project Name:** {project_name}
- **Document Type:** {document_type}
- **Target Audience:** {target_audience}
- **Stakeholders:** {stakeholders}
- **Language:** {language}

## BUSINESS CONTEXT
{business_problem}

## PROJECT DESCRIPTION
{project_description}

{additional_instructions_block}

## SOURCE DOCUMENT CONTENT
The following content was extracted from the uploaded source document(s).
Use this as your primary factual reference — extract specific details, numbers,
processes, system names, and data from here rather than inventing them.

{llm_context}

{previous_sections_block}

---
FORMATTING RULES:
- Write in Markdown format with appropriate headings (## and ###)
- Do NOT include the section title as a top-level heading — start directly with content
- Tables should use Markdown table format
- Lists should use - bullets or numbered lists as appropriate
- Code blocks for schemas, configs, or technical specifications
- Target length: approximately {target_words} words
- Language: {language}
"""

_GENERATION_PROMPT = """Generate the **{section_title}** section.

SECTION INSTRUCTIONS:
{section_instructions}

Write this section now. Start directly with the content — no preamble, no "Here is the section:" opener."""

_REVISION_PROMPT = """The user has reviewed the **{section_title}** section and requested changes.

ORIGINAL SECTION CONTENT:
{previous_content}

USER EDIT REQUEST:
{edit_comment}

SECTION INSTRUCTIONS (for reference):
{section_instructions}

Rewrite the **{section_title}** section incorporating the user's requested changes.
Preserve any parts the user did not ask to change. Start directly with the revised content."""


# ─────────────────────────────────────────────────────────────────────────────
# Public function
# ─────────────────────────────────────────────────────────────────────────────

def generate_section(
    section_key:          str,
    section_title:        str,
    section_instructions: str,
    document_type:        str,
    system_instructions:  str,
    llm_context:          str,
    user_inputs:          dict,
    previous_sections:    list[dict],
    target_words:         int = 300,
    edit_comment:         Optional[str] = None,
    previous_content:     Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Generate (or regenerate) a single section using the LLM.

    Args:
        section_key:          Internal key, e.g. "executive_summary"
        section_title:        Display title, e.g. "Executive Summary"
        section_instructions: Section-specific prompt instructions
        document_type:        e.g. "Business Requirements Document (BRD)"
        system_instructions:  Template-level instructions (tone, style, etc.)
        llm_context:          Full document context from ParsedDocument.to_llm_context()
        user_inputs:          Dict of user-provided metadata (project_name, etc.)
        previous_sections:    List of {"title": ..., "content": ...} already generated
        target_words:         Approximate word count target
        edit_comment:         If set → revision mode (user requested changes)
        previous_content:     The existing section content to revise (required if edit_comment set)

    Returns:
        (content_markdown, full_prompt_used, model_identifier)
    """
    model, model_id, call_kwargs = _get_model()

    # Cap llm_context to reduce prompt size — GPT-5 needs room for reasoning tokens.
    # 8 000 chars ≈ ~2 000 tokens, sufficient for rich document context.
    _MAX_CONTEXT_CHARS = 8_000
    if len(llm_context) > _MAX_CONTEXT_CHARS:
        llm_context = llm_context[:_MAX_CONTEXT_CHARS] + "\n\n[... document truncated for brevity ...]"

    system_prompt = _build_system_prompt(
        document_type        = document_type,
        system_instructions  = system_instructions,
        llm_context          = llm_context,
        user_inputs          = user_inputs,
        previous_sections    = previous_sections,
        target_words         = target_words,
    )

    if edit_comment and previous_content:
        user_prompt = _REVISION_PROMPT.format(
            section_title        = section_title,
            previous_content     = previous_content,
            edit_comment         = edit_comment,
            section_instructions = section_instructions,
        )
    else:
        user_prompt = _GENERATION_PROMPT.format(
            section_title        = section_title,
            section_instructions = section_instructions,
        )

    full_prompt_for_log = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

    import litellm

    # GPT-5 is a reasoning model:
    #   • It ignores system messages (all tokens go to reasoning, content="" when system used)
    #   • It requires max_completion_tokens, not max_tokens
    #   • drop_params=True silently drops any unsupported params (temperature etc.)
    # Fix: merge system + user into a single user message so content is returned correctly.
    litellm.drop_params = True

    combined_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    max_tok = _estimate_max_tokens(target_words)
    logger.info(
        "[gen] Section '%s' — prompt %d chars, max_completion_tokens=%d",
        section_key, len(combined_prompt), max_tok,
    )

    try:
        response = litellm.completion(
            model    = model,
            messages = [
                {"role": "user", "content": combined_prompt},
            ],
            max_completion_tokens = max_tok,
            **call_kwargs,
        )
        content = (response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        reasoning_tok = getattr(usage, "completion_tokens_details", None)
        logger.info(
            "[gen] Section '%s' done — %d words  usage=%s  reasoning_details=%s",
            section_key, len(content.split()), usage, reasoning_tok,
        )
        return content, full_prompt_for_log, model_id

    except Exception as e:
        logger.exception("LLM call failed for section '%s'", section_key)
        raise RuntimeError(f"LLM generation failed for '{section_title}': {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_system_prompt(
    document_type:       str,
    system_instructions: str,
    llm_context:         str,
    user_inputs:         dict,
    previous_sections:   list[dict],
    target_words:        int,
) -> str:
    # Previous sections block — helps LLM maintain coherence and avoid repetition
    if previous_sections:
        prev_block_lines = [
            "\n## SECTIONS ALREADY WRITTEN\n"
            "Do NOT repeat information already covered in these sections:\n"
        ]
        for s in previous_sections:
            # Include only first 150 chars — enough for coherence, keeps prompt lean
            # as more sections accumulate (section 12 would otherwise carry ~4 400 chars)
            preview = s["content"][:150].strip()
            if len(s["content"]) > 150:
                preview += "…"
            prev_block_lines.append(f"### {s['title']}\n{preview}\n")
        previous_sections_block = "\n".join(prev_block_lines)
    else:
        previous_sections_block = ""

    additional = user_inputs.get("additional_instructions", "")
    if additional:
        additional_instructions_block = f"## ADDITIONAL INSTRUCTIONS FROM USER\n{additional}\n"
    else:
        additional_instructions_block = ""

    # Also prepend template system instructions
    if system_instructions:
        additional_instructions_block = (
            f"## DOCUMENT STYLE INSTRUCTIONS\n{system_instructions}\n\n"
            + additional_instructions_block
        )

    return _SYSTEM_TEMPLATE.format(
        document_type                = document_type,
        project_name                 = user_inputs.get("project_name", "Unnamed Project"),
        target_audience              = user_inputs.get("target_audience") or "Business and technical stakeholders",
        stakeholders                 = user_inputs.get("stakeholders") or "Not specified",
        language                     = user_inputs.get("language", "English"),
        business_problem             = user_inputs.get("business_problem") or "Not provided",
        project_description          = user_inputs.get("project_description") or "Not provided",
        additional_instructions_block= additional_instructions_block,
        llm_context                  = llm_context or "(No source document provided)",
        previous_sections_block      = previous_sections_block,
        target_words                 = target_words,
    )


def _estimate_max_tokens(target_words: int) -> int:
    """
    Approximate max_completion_tokens from target word count.

    GPT-5 is a reasoning model: it consumes tokens for *both* internal reasoning
    (invisible) and actual output text (visible).  With a small budget the model
    spends all tokens on reasoning and returns content="".

    Formula: target_words × 6, floor 5 000, ceiling 16 000.
    A 300-word section → 5 000 tokens.  A 600-word section → 3 600 → clamped to 5 000.
    This gives GPT-5 enough headroom for ~2 000–3 000 reasoning tokens plus the full
    output, even with a large prompt (full llm_context + previous sections).
    """
    return max(5000, min(int(target_words * 6), 16000))


def _get_model() -> tuple[str, str, dict]:
    """
    Returns (litellm_model_string, model_identifier, extra_call_kwargs).
    Uses the same provider config as the ADK agent.
    """
    provider = os.environ.get("MODEL_PROVIDER", "azure_gpt5").lower()

    if provider == "azure_gpt5":
        key      = os.environ.get("AZURE_GPT5_OPENAI_API_KEY", "")
        endpoint = os.environ.get("AZURE_GPT5_OPENAI_ENDPOINT", "")
        version  = os.environ.get("AZURE_GPT5_API_VERSION", "2024-12-01-preview")
        deploy   = os.environ.get("AZURE_GPT5_MODEL_DEPLOYMENT_ID", "project-pulse-gpt-5")

        if not key or not endpoint:
            raise EnvironmentError(
                "AZURE_GPT5_OPENAI_API_KEY and AZURE_GPT5_OPENAI_ENDPOINT must be set"
            )
        os.environ.update({
            "AZURE_API_KEY":     key,
            "AZURE_API_BASE":    endpoint,
            "AZURE_API_VERSION": version,
        })
        model_str = f"azure/{deploy}"
        return model_str, model_str, {}

    elif provider == "gemini":
        api_key   = os.environ.get("GOOGLE_API_KEY", "")
        model_str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY must be set for gemini provider")
        return model_str, model_str, {"api_key": api_key}

    elif provider == "azure_openai":
        key      = os.environ.get("AZURE_OPENAI_API_KEY", "")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01")
        deploy   = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        if not key or not endpoint:
            raise EnvironmentError(
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set"
            )
        os.environ.update({
            "AZURE_API_KEY":     key,
            "AZURE_API_BASE":    endpoint,
            "AZURE_API_VERSION": version,
        })
        model_str = f"azure/{deploy}"
        return model_str, model_str, {}

    raise EnvironmentError(
        f"Unknown MODEL_PROVIDER='{provider}'. Use: azure_gpt5 | gemini | azure_openai"
    )
