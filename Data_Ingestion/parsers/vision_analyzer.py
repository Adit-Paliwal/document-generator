"""
Vision AI Image Analyzer
=========================
Sends extracted images to a vision-capable LLM to generate structured descriptions.
Identifies image types (workflow/flowchart, architecture diagram, chart, etc.) and
produces natural-language descriptions suitable for use in document generation prompts.

Controlled by VISION_ENABLED=true/false in .env (default: true).
Gracefully no-ops when disabled or when the LLM call fails.
"""

from __future__ import annotations
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

VISION_ENABLED = os.environ.get("VISION_ENABLED", "true").lower() == "true"
MAX_IMAGE_BYTES_FOR_VISION = 5 * 1024 * 1024  # 5 MB — larger images are downsampled

_PROMPT = """You are analyzing an image extracted from a business document (PDF, PowerPoint, Word, or Excel).

Provide a structured JSON analysis with EXACTLY these fields:

{
  "image_type": "<one value from the list below>",
  "description": "<2-3 sentences describing what this image shows and its business significance>",
  "key_elements": ["<element1>", "<element2>", "<element3>"],
  "contains_text": <true or false>
}

image_type must be ONE of:
- "workflow_flowchart"     — process flows, decision trees, swim-lane diagrams, BPMN diagrams
- "architecture_diagram"  — system architecture, network diagrams, component diagrams, deployment diagrams
- "chart_graph"           — bar charts, line graphs, pie charts, scatter plots, dashboards
- "table_screenshot"      — screenshot of a table or spreadsheet
- "ui_screenshot"         — screenshot of a user interface, application, or website
- "photo"                 — photograph, realistic image, or illustration
- "logo_icon"             — company logo, icon, badge, or small graphic
- "other"                 — anything else

For description:
- workflow_flowchart: Describe the steps, decision points, and overall flow direction
- architecture_diagram: Name the main components, layers, and how they connect
- chart_graph: Describe the data being visualised, axes, and the key trend or insight
- ui_screenshot: Describe the UI component and what action/state it depicts
- other types: Describe the visual content and its apparent business purpose

key_elements: List 3-5 specific items visible in the image (component names, step labels, chart titles, etc.)

Respond with ONLY valid JSON. No markdown, no explanation outside the JSON object."""


def analyze_image(base64_data: str, img_format: str = "png") -> dict:
    """
    Analyze a base64-encoded image using the configured vision-capable LLM.

    Args:
        base64_data: Base64-encoded image bytes (without data URI prefix).
        img_format:  Image format string, e.g. 'png', 'jpeg', 'jpg'.

    Returns:
        Dict with keys: image_type, description, key_elements, contains_text.
        Empty dict if vision is disabled or the call fails.
    """
    if not VISION_ENABLED:
        return {}

    if not base64_data:
        return {}

    # Rough byte check — skip if too large (prevent API timeout)
    approx_bytes = len(base64_data) * 3 // 4
    if approx_bytes > MAX_IMAGE_BYTES_FOR_VISION:
        logger.info(
            "Skipping vision analysis: image too large (~%d MB)",
            approx_bytes // (1024 * 1024),
        )
        return {}

    try:
        import litellm

        model, kwargs = _get_model_config()
        if not model:
            logger.warning("Vision analysis skipped: no vision model configured")
            return {}

        mime = f"image/{img_format.lower().replace('jpg', 'jpeg')}"

        # GPT-5 / reasoning models: drop_params=True handles temperature & unsupported params
        litellm.drop_params = True

        response = litellm.completion(
            model    = model,
            messages = [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url":    f"data:{mime};base64,{base64_data}",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "text",
                        "text": _PROMPT,
                    },
                ],
            }],
            max_completion_tokens = 512,
            # temperature and response_format omitted — GPT-5 doesn't support them
            **kwargs,
        )

        raw = response.choices[0].message.content.strip()
        result = _parse_json(raw)
        if not result:
            return {}

        # Validate and normalise
        valid_types = {
            "workflow_flowchart", "architecture_diagram", "chart_graph",
            "table_screenshot", "ui_screenshot", "photo", "logo_icon", "other",
        }
        if result.get("image_type") not in valid_types:
            result["image_type"] = "other"

        result["key_elements"] = result.get("key_elements") or []
        result["contains_text"] = bool(result.get("contains_text", False))

        logger.info(
            "Vision analysis: type=%s  elements=%s",
            result.get("image_type"),
            result.get("key_elements"),
        )
        return result

    except Exception as e:
        logger.warning("Vision analysis failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_model_config() -> tuple[Optional[str], dict]:
    """
    Returns (litellm_model_string, extra_kwargs) based on MODEL_PROVIDER env var.
    Returns (None, {}) if the provider is not configured for vision.
    """
    provider = os.environ.get("MODEL_PROVIDER", "azure_gpt5").lower()

    if provider == "azure_gpt5":
        key      = os.environ.get("AZURE_GPT5_OPENAI_API_KEY", "")
        endpoint = os.environ.get("AZURE_GPT5_OPENAI_ENDPOINT", "")
        version  = os.environ.get("AZURE_GPT5_API_VERSION", "2024-12-01-preview")
        deploy   = os.environ.get("AZURE_GPT5_MODEL_DEPLOYMENT_ID", "project-pulse-gpt-5")
        if not key or not endpoint:
            return None, {}
        os.environ.update({
            "AZURE_API_KEY":     key,
            "AZURE_API_BASE":    endpoint,
            "AZURE_API_VERSION": version,
        })
        return f"azure/{deploy}", {}

    elif provider == "gemini":
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        model   = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        if not api_key:
            return None, {}
        return model, {"api_key": api_key}

    elif provider == "azure_openai":
        key      = os.environ.get("AZURE_OPENAI_API_KEY", "")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01")
        deploy   = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        if not key or not endpoint:
            return None, {}
        os.environ.update({
            "AZURE_API_KEY":     key,
            "AZURE_API_BASE":    endpoint,
            "AZURE_API_VERSION": version,
        })
        return f"azure/{deploy}", {}

    logger.warning("Unknown MODEL_PROVIDER '%s' — vision disabled", provider)
    return None, {}


def _parse_json(raw: str) -> Optional[dict]:
    """Extract JSON from LLM response, handling markdown code fences."""
    text = raw.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first and last fence lines
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object within the response
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    logger.warning("Could not parse vision JSON: %s", raw[:200])
    return None
