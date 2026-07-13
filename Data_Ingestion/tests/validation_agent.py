"""
Shim — the Validation Agent moved to generation/validation_agent.py when it
became runtime code (POST /api/generate/{job_id}/validate uses it).
All names re-exported so existing test imports keep working.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BASE = Path(__file__).parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from generation.validation_agent import (   # noqa: F401,E402
    GROUNDING_THRESHOLD, LONG_TEXT_CHARS, PASS_THRESHOLD, SEMANTIC_THRESHOLD, WEIGHTS,
    EdgeCheck, Finding, Provenance, Report, SourceDoc, ValidationAgent,
    text_similarity,
)

if __name__ == "__main__":
    import runpy
    runpy.run_module("generation.validation_agent", run_name="__main__")
