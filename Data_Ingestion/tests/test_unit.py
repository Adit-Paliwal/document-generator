"""
Unit + edge-case tests — no server, no LLM, no network.
Run:  cd Data_Ingestion && python -m pytest tests/test_unit.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_BASE = Path(__file__).parent.parent.resolve()
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from generation import ontology as onto                      # noqa: E402
from generation.generator import _build_system_prompt, _estimate_max_tokens   # noqa: E402
from generation.generation_service import project_review_rollup               # noqa: E402
from generation.review_service import (                      # noqa: E402
    _comments_fingerprint, _extract_json, _validate_persona_review,
)


# ═════════════════════════════════════════════════════════════════════════════
# Ontology — doc-type normalisation
# ═════════════════════════════════════════════════════════════════════════════

class TestOntologyDocType:
    @pytest.mark.parametrize("raw,expected", [
        ("BRD", "BRD"),
        ("brd", "BRD"),
        ("Business Requirements Document (BRD)", "BRD"),
        ("Note for Approval", "NFA"),          # full-form fallback
        ("NIT", "NIT"),
        ("ndpr", "NDPR"),
        ("SOW", None),                          # not in ontology
        ("", None),                             # empty
        ("XBRDX", None),                        # word boundary — no substring match
    ])
    def test_normalisation(self, raw, expected):
        assert onto._norm_doc_type(raw) == expected

    def test_none_input(self):
        assert onto._norm_doc_type(None) is None


# ═════════════════════════════════════════════════════════════════════════════
# Ontology — selective matching (the token-discipline contract)
# ═════════════════════════════════════════════════════════════════════════════

class TestOntologyMatching:
    def test_glossary_matches_only_present_terms(self):
        block = onto.glossary_block("The ABT metering rollout for AMI data.")
        assert "Availability Based Tariff" in block
        assert "Advanced Metering Infrastructure" in block
        assert "SCADA" not in block            # not mentioned → not injected

    def test_glossary_word_boundaries(self):
        # 'AI' must not match inside 'MAINTAIN'; 'BRD' not inside 'XBRDX'
        block = onto.glossary_block("WE MAINTAIN THE XBRDX SYSTEM")
        assert "Artificial Intelligence" not in block
        assert "Business Requirment Document" not in block

    def test_glossary_unicode_key(self):
        block = onto.glossary_block("Monitor the ΔT (Delta‑T) of the chillers")
        assert "temperature difference" in block

    def test_glossary_empty_and_no_match(self):
        assert onto.glossary_block("") == ""
        assert onto.glossary_block("nothing relevant here at all") == ""

    def test_glossary_cap_enforced(self):
        all_terms_text = " ".join(list(onto._load()["terms"].keys()))
        block = onto.glossary_block(all_terms_text, limit=10)
        assert block.count("\n- ") <= 10 + 1

    def test_tech_landscape_matching_and_cap(self):
        block = onto.tech_landscape_block("We integrate SAP IS-U with GCP BigQuery", limit=5)
        assert "SAP IS-U" in block and "BigQuery" in block
        assert onto.tech_landscape_block("no known systems named") == ""

    def test_company_context_entity_selection(self):
        assert "AEML" in onto.company_context("project for Mumbai consumers")
        assert "AESL" in onto.company_context("")     # default entity

    def test_assemblies_size_budget(self):
        big_scan = "AEML Mumbai SAP IS-U SCADA AMI MDM ABT MERC BigQuery " * 50
        for block in (onto.for_generation("BRD", big_scan),
                      onto.for_derivation(big_scan),
                      onto.for_extraction(big_scan)):
            assert 0 < len(block) <= 8000, "ontology block exceeded token budget"

    def test_review_assembly(self):
        block = onto.for_review("NFA")
        assert "Inputs it must cover" in block
        assert onto.for_review("Scope Document") == ""     # unknown → graceful empty


# ═════════════════════════════════════════════════════════════════════════════
# Review service — persona-review validator (LLM output gate)
# ═════════════════════════════════════════════════════════════════════════════

VALID_IDS = {"s1", "s2"}
TITLES = {"s1": "Purpose", "s2": "Budget"}

class TestValidatePersonaReview:
    def test_happy_path(self):
        parsed = {"summary": "Solid NFA overall.",
                  "section_comments": [{"section_id": "s1", "severity": "high", "comment": "Add cost breakup"}]}
        summary, comments = _validate_persona_review(parsed, VALID_IDS, TITLES)
        assert summary == "Solid NFA overall."
        assert comments[0]["severity"] == "high"
        assert comments[0]["section_title"] == "Purpose"

    @pytest.mark.parametrize("bad", [
        None,                                   # unparseable
        "just a string",                        # wrong type
        {},                                     # no summary
        {"summary": ""},                        # empty summary
        {"summary": "ok", "section_comments": "not-a-list"},   # wrong container type
    ])
    def test_rejects_malformed(self, bad):
        assert _validate_persona_review(bad, VALID_IDS, TITLES) is None

    def test_drops_bad_items_keeps_good(self):
        parsed = {"summary": "ok", "section_comments": [
            {"section_id": "s1", "comment": "keep me"},
            {"section_id": "UNKNOWN", "comment": "wrong id"},      # dropped
            {"section_id": "s2", "comment": "   "},                # empty text → dropped
            "not-a-dict",                                          # dropped
            {"section_id": "s2", "severity": "BOGUS", "comment": "bad severity"},
        ]}
        _, comments = _validate_persona_review(parsed, VALID_IDS, TITLES)
        assert len(comments) == 2
        assert comments[1]["severity"] == "medium"     # invalid severity normalised

    def test_missing_comments_key_is_ok(self):
        assert _validate_persona_review({"summary": "fine"}, VALID_IDS, TITLES) == ("fine", [])

    def test_unicode_content(self):
        parsed = {"summary": "बजट अनुमान ₹12.5 Cr — ठीक है ✓",
                  "section_comments": [{"section_id": "s1", "comment": "लागत split करें 💰"}]}
        summary, comments = _validate_persona_review(parsed, VALID_IDS, TITLES)
        assert "₹12.5" in summary and "💰" in comments[0]["comment"]


class TestExtractJson:
    @pytest.mark.parametrize("raw,ok", [
        ('{"a": 1}', True),
        ('```json\n{"a": 1}\n```', True),                    # fenced
        ('Here is the JSON you asked for: {"a": 1} hope it helps', True),   # prose-wrapped
        ('{"a": 1',  False),                                 # truncated / corrupted
        ("", False),
        (None, False),
        ("no json at all", False),
    ])
    def test_extraction(self, raw, ok):
        result = _extract_json(raw)
        assert (result == {"a": 1}) if ok else (result is None)


class TestCommentsFingerprint:
    def _c(self, cid, text, ts=None):
        return SimpleNamespace(comment_id=cid, text=text, updated_at=ts)

    def test_stable_and_order_insensitive(self):
        a, b = self._c("1", "alpha"), self._c("2", "beta")
        assert _comments_fingerprint([a, b]) == _comments_fingerprint([b, a])

    def test_changes_on_edit_and_addition(self):
        base = [self._c("1", "alpha")]
        assert _comments_fingerprint(base) != _comments_fingerprint([self._c("1", "alpha EDITED")])
        assert _comments_fingerprint(base) != _comments_fingerprint(base + [self._c("2", "new")])

    def test_empty(self):
        assert isinstance(_comments_fingerprint([]), str)


# ═════════════════════════════════════════════════════════════════════════════
# Generator — prompt template integrity
# ═════════════════════════════════════════════════════════════════════════════

class TestSystemPrompt:
    def _build(self, **over):
        kw = dict(
            document_type="NFA", system_instructions="", llm_context="AEML SAP IS-U context",
            user_inputs={"project_name": "BDR"}, previous_sections=[], target_words=300,
        )
        kw.update(over)
        return _build_system_prompt(**kw)

    def test_no_unresolved_placeholders(self):
        p = self._build()
        assert "{ontology_block}" not in p and "{llm_context}" not in p

    def test_ontology_embedded(self):
        p = self._build()
        assert "DOCUMENT-TYPE GUIDANCE" in p and "ORGANISATION CONTEXT" in p

    def test_previous_sections_truncated_to_preview(self):
        p = self._build(previous_sections=[{"title": "Long", "content": "x" * 5000}])
        assert "x" * 151 not in p          # 150-char preview cap holds
        assert "SECTIONS ALREADY WRITTEN" in p

    def test_empty_inputs_safe(self):
        p = self._build(llm_context="", user_inputs={})
        assert "(No source document provided)" in p and "Unnamed Project" in p

    @pytest.mark.parametrize("words,expected", [(10, 5000), (300, 5000), (2000, 12000), (99999, 16000)])
    def test_max_tokens_bounds(self, words, expected):
        assert _estimate_max_tokens(words) == expected


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard rollup — status aggregation
# ═════════════════════════════════════════════════════════════════════════════

class TestReviewRollup:
    def _j(self, status):
        return SimpleNamespace(review_status=status)

    @pytest.mark.parametrize("statuses,expected", [
        ([], "under_draft"),
        (["draft"], "under_draft"),
        ([None], "under_draft"),                              # null review_status
        (["approved"], "approved"),
        (["approved", "approved"], "approved"),
        (["approved", "draft"], "under_draft"),               # mixed w/o review activity
        (["under_review"], "under_review"),
        (["approved", "rejected"], "under_review"),           # rejected forces rework
        (["revision_requested", "draft"], "under_review"),
    ])
    def test_rollup(self, statuses, expected):
        assert project_review_rollup([self._j(s) for s in statuses]) == expected


# ═════════════════════════════════════════════════════════════════════════════
# Performance sanity — hot paths must stay cheap
# ═════════════════════════════════════════════════════════════════════════════

class TestPerformanceSanity:
    def test_glossary_scan_1mb_under_3s(self):
        huge = ("The AMI rollout with SAP IS-U, SCADA telemetry and MERC filings. " * 16000)  # ~1MB
        t0 = time.perf_counter()
        block = onto.glossary_block(huge)
        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, f"glossary scan took {elapsed:.2f}s on 1MB input"
        assert "Advanced Metering Infrastructure" in block

    def test_prompt_build_under_100ms_after_warmup(self):
        _build_system_prompt(document_type="BRD", system_instructions="", llm_context="ctx",
                             user_inputs={}, previous_sections=[], target_words=300)   # warm ontology cache
        t0 = time.perf_counter()
        for _ in range(10):
            _build_system_prompt(document_type="BRD", system_instructions="", llm_context="ctx " * 500,
                                 user_inputs={"project_name": "p"}, previous_sections=[], target_words=300)
        assert (time.perf_counter() - t0) / 10 < 0.1
