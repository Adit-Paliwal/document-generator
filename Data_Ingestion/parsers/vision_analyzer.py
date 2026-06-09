"""
Vision AI Image Analyzer
=========================
Sends extracted images to a vision-capable LLM to generate structured descriptions.
Identifies image types (workflow/flowchart, architecture diagram, chart, etc.) and
produces natural-language descriptions suitable for use in document generation prompts.

Provider order (same as the rest of the application):
  1. Gemini 2.5 Flash on Vertex AI  — primary (via llm_provider)
  2. Azure GPT-5                    — automatic fallback (via llm_provider)

Controlled by VISION_ENABLED=true/false in .env (default: true).
Gracefully no-ops when disabled or when the LLM call fails — uploads never break
due to a vision analysis failure.
"""

from __future__ import annotations
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Ensure Data_Ingestion/ is on sys.path so llm_provider can be imported
_BASE = Path(__file__).parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

logger = logging.getLogger(__name__)

VISION_ENABLED             = os.environ.get("VISION_ENABLED", "true").lower() == "true"
MAX_IMAGE_BYTES_FOR_VISION = 5 * 1024 * 1024   # 5 MB — larger images are skipped

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
    Uses Gemini 2.5 Flash (Vertex AI) as primary, Azure GPT-5 as fallback.

    Args:
        base64_data: Base64-encoded image bytes (without data URI prefix).
        img_format:  Image format string, e.g. 'png', 'jpeg', 'jpg'.

    Returns:
        Dict with keys: image_type, description, key_elements, contains_text.
        Empty dict if vision is disabled or the call fails — never raises.
    """
    if not VISION_ENABLED:
        return {}

    if not base64_data:
        return {}

    # Rough byte check — skip images that are too large to avoid API timeouts
    approx_bytes = len(base64_data) * 3 // 4
    if approx_bytes > MAX_IMAGE_BYTES_FOR_VISION:
        logger.info(
            "[Vision] Skipping vision analysis: image too large (~%d MB)",
            approx_bytes // (1024 * 1024),
        )
        return {}

    try:
        from llm_provider import call_vision_with_fallback

        mime = f"image/{img_format.lower().replace('jpg', 'jpeg')}"

        raw, provider = call_vision_with_fallback(
            text_prompt = _PROMPT,
            base64_data = base64_data,
            mime_type   = mime,
            max_tokens  = 512,
            timeout     = 30,
            log_prefix  = "[Vision]",
        )

        result = _parse_json(raw)
        if not result:
            return {}

        # Validate and normalise image_type
        valid_types = {
            "workflow_flowchart", "architecture_diagram", "chart_graph",
            "table_screenshot", "ui_screenshot", "photo", "logo_icon", "other",
        }
        if result.get("image_type") not in valid_types:
            result["image_type"] = "other"

        result["key_elements"]  = result.get("key_elements") or []
        result["contains_text"] = bool(result.get("contains_text", False))

        logger.info(
            "[Vision] ✓ type=%s  elements=%s  provider=%s",
            result.get("image_type"),
            result.get("key_elements"),
            provider,
        )
        return result

    except Exception as e:
        logger.warning("[Vision] Analysis failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# JSON parser — handles markdown fences and partial JSON objects
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> Optional[dict]:
    """Extract JSON from LLM response, handling markdown code fences."""
    text = raw.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        lines = text.split("\n")
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

    logger.warning("[Vision] Could not parse JSON from response: %s", raw[:200])
    return None
