"""
Validation Agent — scores generated output against ground truth AND traces
every piece of content back to its source document.
==========================================================================
Compares semantically and structurally, ignoring non-functional differences
(whitespace, key ordering), and produces a weighted 0-100 score:

    correctness 40% · completeness 20% · format 15% · edge cases 15% · robustness 10%

Source-document provenance:
    Pass `source_documents=[SourceDoc(name=..., path=..., content=...)]` and
    every long-text field in the generated output is attributed to the
    attached document that best supports it — the report's `provenance` list
    carries the SOURCE DOCUMENT NAME AND PATH per field plus a 0-1 support
    score. Content not traceable to any attached document is marked
    grounded=false (source: form data / AI-derived) and flagged.

    `ground_truth` may be None: the agent then validates a fresh generated
    document purely on grounding + completeness + the supplied checks
    (this is what POST /api/generate/{job_id}/validate uses).

Usage:
    from generation.validation_agent import ValidationAgent, EdgeCheck, SourceDoc

    agent  = ValidationAgent()                    # deterministic (offline)
    agent  = ValidationAgent(use_llm=True)        # + Gemini semantic judge
    report = agent.evaluate(generated, ground_truth,
                            required_fields=[...],
                            source_documents=[SourceDoc(...)],
                            edge_checks=[EdgeCheck("empty list ok", ok=True)],
                            robustness_checks=[...])
    report.score, report.passed, report.findings, report.provenance, report.to_dict()

Verdict: PASS iff score >= PASS_THRESHOLD (80) and no CRITICAL finding.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Data_Ingestion on sys.path (for the optional LLM judge)
_BASE = Path(__file__).parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

PASS_THRESHOLD = 80
SEMANTIC_THRESHOLD = 0.55       # similarity above this = semantically equivalent
LONG_TEXT_CHARS = 40            # strings longer than this use semantic comparison
GROUNDING_THRESHOLD = 0.15      # salient-token support below this = not traceable to sources
GROUNDING_FULL_CREDIT = 0.40    # support at/above this earns full grounding credit

WEIGHTS = {
    "correctness": 40,
    "completeness": 20,
    "format": 15,
    "edge_cases": 15,
    "robustness": 10,
}


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    category: str      # missing_field | extra_field | incorrect_value | type_mismatch |
                       # format_error | edge_case_failure | error_handling
    path: str          # e.g. "extracted.project_name"
    message: str
    severity: str = "MAJOR"   # CRITICAL | MAJOR | MINOR


@dataclass
class EdgeCheck:
    """An observed edge-case behaviour, graded by the agent."""
    name: str
    ok: bool
    detail: str = ""
    critical: bool = False


@dataclass
class SourceDoc:
    """An attached source document the generated output should trace back to."""
    name: str                  # e.g. "AEML_Demand_Response.docx"
    path: str                  # storage path / volume path of the document
    content: str               # parsed text (ParsedDocument.to_llm_context())


@dataclass
class Provenance:
    """Where one generated field's content came from."""
    field: str
    grounded: bool
    support: float             # 0..1 — fraction of salient tokens found in the source
    source_name: str | None    # attached document name, or None
    source_path: str | None    # attached document path, or None
    origin: str                # "attached_document" | "form_data_or_derived"


@dataclass
class Report:
    score: float
    metrics: dict
    findings: list
    passed: bool
    summary: str
    provenance: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "passed": self.passed,
            "metrics": {k: round(v, 1) for k, v in self.metrics.items()},
            "findings": [vars(f) for f in self.findings],
            "provenance": [
                {**vars(p), "support": round(p.support, 3)} for p in self.provenance
            ],
            "summary": self.summary,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation & similarity
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(value: Any) -> Any:
    """Parse JSON strings; collapse whitespace in text. Key order in dicts is
    inherently ignored (dict comparison is order-insensitive)."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "{[":
            try:
                return _normalize(json.loads(s))
            except Exception:
                pass
        return re.sub(r"\s+", " ", s)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
              "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _stem(tok: str) -> str:
    """Crude stemmer so morphological variants compare equal:
    takes/taking→tak, manually→manual, brds→brd, two→2."""
    if tok in _NUM_WORDS:
        return _NUM_WORDS[tok]
    for suf in ("ing", "ed", "ly", "es", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            tok = tok[: -len(suf)]
            break
    return tok[:-1] if len(tok) > 4 and tok.endswith("e") else tok


_STOPWORDS = frozenset(
    "the and for with that this from will shall must should would could their there "
    "which where when what these those been being have has had are was were its "
    "into onto within without across during between through against".split()
)


def _salient_tokens(text: str) -> set[str]:
    """Content-bearing tokens for grounding: numbers, acronyms, and stemmed
    words of length >= 5 (minus stopwords). Generic filler doesn't count as
    evidence that content came from a source document."""
    if not text:
        return set()
    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z\-]{1,}|\d[\d,.]*", text):
        if re.match(r"^\d", tok):
            out.add(tok.rstrip(".,"))            # numbers: dates, costs, KPIs
        elif tok.isupper() and len(tok) >= 2:
            out.add(tok)                          # acronyms: AEML, SCADA, MERC
        elif len(tok) >= 5:
            low = tok.lower()
            if low not in _STOPWORDS:
                out.add(_stem(low))
    return out


def _iter_text_fields(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Yield (path, text) for every long-text leaf in a dict/list structure."""
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += _iter_text_fields(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += _iter_text_fields(v, f"{path}[{i}]")
    elif isinstance(value, str) and len(value) > LONG_TEXT_CHARS:
        out.append((path or "$", value))
    return out


def text_similarity(a: str, b: str) -> float:
    """Deterministic semantic proxy: blend of stemmed token-set F1 and sequence ratio."""
    ta = {_stem(t) for t in re.findall(r"[a-z0-9]+", a.lower())}
    tb = {_stem(t) for t in re.findall(r"[a-z0-9]+", b.lower())}
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    p, r = inter / len(tb), inter / len(ta)
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    seq = difflib.SequenceMatcher(None, a.lower()[:2000], b.lower()[:2000]).ratio()
    return 0.6 * f1 + 0.4 * seq


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class ValidationAgent:
    def __init__(self, use_llm: bool = False):
        self.use_llm = use_llm

    # ── public API ────────────────────────────────────────────────────────────
    def evaluate(
        self,
        generated: Any,
        ground_truth: Any = None,
        *,
        required_fields: Optional[list[str]] = None,
        exact_fields: Optional[list[str]] = None,       # force exact match on these paths
        source_documents: Optional[list[SourceDoc]] = None,
        edge_checks: Optional[list[EdgeCheck]] = None,
        robustness_checks: Optional[list[EdgeCheck]] = None,
        allow_extra_fields: bool = True,
    ) -> Report:
        findings: list[Finding] = []
        gen = _normalize(generated)
        gt = _normalize(ground_truth)

        # Source-document provenance — attribute every long-text field to the
        # attached document that supports it (name + PATH in the report).
        provenance: list[Provenance] = []
        grounding_credit: Optional[float] = None
        if source_documents:
            provenance, grounding_credit = self._grounding(gen, source_documents, findings)

        fmt = self._score_format(gen, gt, findings)
        if gt is not None:
            correctness = self._score_correctness(gen, gt, exact_fields or [], findings, path="")
            if grounding_credit is not None:
                correctness = 0.7 * correctness + 0.3 * grounding_credit
        else:
            # No ground truth (fresh generated document): correctness = how well
            # the content traces back to the attached sources.
            correctness = grounding_credit if grounding_credit is not None else 1.0
        completeness = self._score_completeness(gen, gt if gt is not None else gen,
                                                required_fields, findings,
                                                allow_extra_fields=allow_extra_fields)
        edge = self._score_checks(edge_checks, findings, "edge_case_failure")
        robust = self._score_checks(robustness_checks, findings, "error_handling")

        metrics = {
            "correctness": correctness * 100,
            "completeness": completeness * 100,
            "format": fmt * 100,
            "edge_cases": edge * 100,
            "robustness": robust * 100,
        }
        score = sum(metrics[k] * WEIGHTS[k] for k in WEIGHTS) / sum(WEIGHTS.values())
        has_critical = any(f.severity == "CRITICAL" for f in findings)
        passed = score >= PASS_THRESHOLD and not has_critical

        grounded_n = sum(1 for p in provenance if p.grounded)
        summary = (
            f"{'PASS' if passed else 'FAIL'} — score {score:.1f}/100 "
            f"({len(findings)} finding(s), "
            f"{sum(1 for f in findings if f.severity == 'CRITICAL')} critical"
            + (f"; {grounded_n}/{len(provenance)} fields traced to attached documents"
               if provenance else "")
            + "). "
            + " · ".join(f"{k}={metrics[k]:.0f}" for k in WEIGHTS)
        )
        return Report(score=score, metrics=metrics, findings=findings, passed=passed,
                      summary=summary, provenance=provenance)

    # ── source-document grounding / provenance ───────────────────────────────
    def _grounding(self, gen: Any, sources: list[SourceDoc],
                   findings: list) -> tuple[list, float]:
        """For every long-text field in `gen`, find the attached document that
        best supports its salient content. Returns (provenance, credit 0..1)."""
        src_tokens = [(s, _salient_tokens(s.content)) for s in sources]
        provenance: list[Provenance] = []
        credits: list[float] = []

        for fpath, text in _iter_text_fields(gen):
            toks = _salient_tokens(text)
            if not toks:
                continue
            best_doc, best_support = None, 0.0
            for doc, dtoks in src_tokens:
                support = len(toks & dtoks) / len(toks)
                if support > best_support:
                    best_doc, best_support = doc, support
            grounded = best_support >= GROUNDING_THRESHOLD and best_doc is not None
            provenance.append(Provenance(
                field=fpath,
                grounded=grounded,
                support=best_support,
                source_name=best_doc.name if grounded else None,
                source_path=best_doc.path if grounded else None,
                origin="attached_document" if grounded else "form_data_or_derived",
            ))
            credits.append(min(1.0, best_support / GROUNDING_FULL_CREDIT))
            if not grounded:
                findings.append(Finding(
                    "ungrounded_content", fpath,
                    f"content not traceable to any attached source document "
                    f"(best support {best_support:.2f}) — verify it against form data",
                    "MINOR"))
        return provenance, (sum(credits) / len(credits) if credits else 1.0)

    # ── metric: format (15%) ─────────────────────────────────────────────────
    def _score_format(self, gen: Any, gt: Any, findings: list) -> float:
        if gt is None:
            return 1.0
        if type(gen) is not type(gt) and not (isinstance(gen, (int, float)) and isinstance(gt, (int, float))):
            findings.append(Finding("format_error", "$",
                                    f"expected {type(gt).__name__}, got {type(gen).__name__}", "CRITICAL"))
            return 0.0
        return 1.0

    # ── metric: correctness (40%) ────────────────────────────────────────────
    def _score_correctness(self, gen: Any, gt: Any, exact_fields: list[str],
                           findings: list, path: str) -> float:
        """Recursive field-by-field comparison. Returns fraction correct [0..1]."""
        if isinstance(gt, dict):
            if not isinstance(gen, dict):
                return 0.0
            if not gt:
                return 1.0
            scores = []
            for k, gt_v in gt.items():
                sub = f"{path}.{k}" if path else k
                if k not in gen:
                    scores.append(0.0)      # completeness also flags this
                    continue
                scores.append(self._score_correctness(gen[k], gt_v, exact_fields, findings, sub))
            return sum(scores) / len(scores)

        if isinstance(gt, list):
            if not isinstance(gen, list):
                findings.append(Finding("type_mismatch", path or "$", "expected list", "MAJOR"))
                return 0.0
            if not gt:
                return 1.0
            if len(gen) != len(gt):
                findings.append(Finding("incorrect_value", path or "$",
                                        f"list length {len(gen)} != expected {len(gt)}", "MINOR"))
            scores = [
                self._score_correctness(gv, tv, exact_fields, findings, f"{path}[{i}]")
                for i, (gv, tv) in enumerate(zip(gen, gt))
            ]
            return (sum(scores) / len(gt)) if gt else 1.0

        # ── leaf ──
        if gen == gt:
            return 1.0
        if isinstance(gt, str) and isinstance(gen, str) and path not in exact_fields \
                and len(gt) > LONG_TEXT_CHARS:
            sim = self._semantic(gen, gt)
            if sim >= SEMANTIC_THRESHOLD:
                return min(1.0, 0.7 + 0.3 * sim)     # equivalent, small penalty vs exact
            findings.append(Finding("incorrect_value", path or "$",
                                    f"text not semantically equivalent (similarity {sim:.2f})", "MAJOR"))
            return max(0.0, sim * 0.5)
        if type(gen) is not type(gt) and not (isinstance(gen, (int, float)) and isinstance(gt, (int, float))):
            findings.append(Finding("type_mismatch", path or "$",
                                    f"{type(gt).__name__} expected, got {type(gen).__name__}", "MAJOR"))
            return 0.0
        findings.append(Finding("incorrect_value", path or "$",
                                f"expected {gt!r}, got {gen!r}", "MAJOR"))
        return 0.0

    # ── metric: completeness (20%) ───────────────────────────────────────────
    def _score_completeness(self, gen: Any, gt: Any, required: Optional[list[str]],
                            findings: list, allow_extra_fields: bool) -> float:
        if not isinstance(gt, dict) or not isinstance(gen, dict):
            return 1.0 if gen is not None else 0.0
        # EVERY ground-truth field is expected; fields in `required` escalate
        # a miss to CRITICAL (auto-fail). Spec: "flag missing fields" — all of them.
        keys = list(dict.fromkeys(list(gt.keys()) + list(required or [])))
        if not keys:
            return 1.0
        present = 0
        for k in keys:
            v = gen.get(k)
            if k in gen and v not in (None, "", [], {}):
                present += 1
            else:
                findings.append(Finding(
                    "missing_field", k,
                    "required field missing or empty" if (required and k in required)
                    else "ground-truth field missing or empty",
                    "CRITICAL" if (required and k in required) else "MAJOR"))
        extra = set(gen.keys()) - set(gt.keys())
        for k in sorted(extra):
            findings.append(Finding("extra_field", k, "field not in ground truth", "MINOR"))
        ratio = present / len(keys)
        if extra and not allow_extra_fields:
            ratio = max(0.0, ratio - 0.1 * len(extra))
        return ratio

    # ── metrics: edge cases (15%) / robustness (10%) ─────────────────────────
    def _score_checks(self, checks: Optional[list[EdgeCheck]], findings: list,
                      category: str) -> float:
        if not checks:
            return 1.0     # nothing claimed, nothing to penalise
        ok = 0
        for c in checks:
            if c.ok:
                ok += 1
            else:
                findings.append(Finding(category, c.name, c.detail or "check failed",
                                        "CRITICAL" if c.critical else "MAJOR"))
        return ok / len(checks)

    # ── semantic comparison (deterministic, optional LLM judge) ─────────────
    def _semantic(self, generated: str, truth: str) -> float:
        base = text_similarity(generated, truth)
        if not self.use_llm:
            return base
        try:
            from llm_provider import call_with_fallback
            raw, _ = call_with_fallback(
                messages=[{"role": "user", "content": (
                    "Are these two texts semantically equivalent (same facts, same intent)? "
                    'Return ONLY JSON: {"similarity": <0..1>, "equivalent": <bool>}\n\n'
                    f"TEXT A (ground truth):\n{truth[:2000]}\n\nTEXT B (generated):\n{generated[:2000]}"
                )}],
                max_tokens=200, timeout=60, log_prefix="[ValidationAgent]", json_mode=True,
            )
            sim = float(json.loads(raw).get("similarity", base))
            return 0.5 * base + 0.5 * max(0.0, min(1.0, sim))
        except Exception:
            return base    # judge unavailable → deterministic fallback


# ─────────────────────────────────────────────────────────────────────────────
# CLI: python tests/validation_agent.py generated.json truth.json
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    gen = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    gt = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    report = ValidationAgent().evaluate(gen, gt)
    print(json.dumps(report.to_dict(), indent=2))
    sys.exit(0 if report.passed else 1)
