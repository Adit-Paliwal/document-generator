"""
Tests for the Validation Agent itself + the reference SAMPLE EVALUATION
(mock generated output vs ground truth for the document-extraction contract).
Run:  python -m pytest tests/test_validation_agent.py -q -s
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from tests.validation_agent import (   # noqa: E402
    EdgeCheck, PASS_THRESHOLD, Report, SourceDoc, ValidationAgent, text_similarity,
)


# ═════════════════════════════════════════════════════════════════════════════
# Agent mechanics
# ═════════════════════════════════════════════════════════════════════════════

class TestAgentMechanics:
    def setup_method(self):
        self.agent = ValidationAgent()

    def test_identical_output_scores_100(self):
        gt = {"a": 1, "b": "text", "c": [1, 2]}
        r = self.agent.evaluate(dict(gt), gt)
        assert r.score == 100 and r.passed and not r.findings

    def test_key_order_and_whitespace_ignored(self):
        gt  = {"x": "hello   world", "y": 1}
        gen = {"y": 1, "x": "hello world"}          # different order + whitespace
        r = self.agent.evaluate(gen, gt)
        assert r.score == 100, r.summary

    def test_json_string_normalised(self):
        r = self.agent.evaluate('{"a": 1}', {"a": 1})
        assert r.metrics["correctness"] == 100

    def test_missing_field_flagged_critical_when_required(self):
        r = self.agent.evaluate({"a": 1}, {"a": 1, "b": 2}, required_fields=["a", "b"])
        cats = {(f.category, f.severity) for f in r.findings}
        assert ("missing_field", "CRITICAL") in cats
        assert not r.passed                          # critical ⇒ fail regardless of score

    def test_extra_field_flagged_minor(self):
        r = self.agent.evaluate({"a": 1, "zz": "surplus"}, {"a": 1})
        assert any(f.category == "extra_field" and f.severity == "MINOR" for f in r.findings)
        assert r.passed                              # extras alone don't fail the bar

    def test_incorrect_scalar_flagged(self):
        r = self.agent.evaluate({"a": 2}, {"a": 1})
        assert any(f.category == "incorrect_value" for f in r.findings)
        assert r.metrics["correctness"] == 0

    def test_type_mismatch_root_is_format_critical(self):
        r = self.agent.evaluate(["not", "a", "dict"], {"a": 1})
        assert any(f.category == "format_error" and f.severity == "CRITICAL" for f in r.findings)
        assert not r.passed

    def test_semantic_equivalence_for_long_text(self):
        gt  = {"desc": "Manual BRD drafting takes two to three weeks per document and causes delays"}
        gen = {"desc": "Drafting BRDs manually causes delays, taking 2-3 weeks per document"}
        r = self.agent.evaluate(gen, gt)
        assert r.metrics["correctness"] >= 70, r.summary   # equivalent, not penalised as wrong

    def test_semantic_divergence_detected(self):
        gt  = {"desc": "The project reduces peak electricity demand through customer incentives and behavioural nudges"}
        gen = {"desc": "A mobile game about farming vegetables with weekly leaderboard tournaments"}
        r = self.agent.evaluate(gen, gt)
        assert any("not semantically equivalent" in f.message for f in r.findings)

    def test_exact_fields_never_use_semantics(self):
        gt  = {"project_code": "a very long exact code that would trip semantic matching XYZ-1"}
        gen = {"project_code": "a very long exact code that would trip semantic matching XYZ-2"}
        r = self.agent.evaluate(gen, gt, exact_fields=["project_code"])
        assert r.metrics["correctness"] < 100

    def test_edge_and_robustness_checks_weighting(self):
        r = self.agent.evaluate({"a": 1}, {"a": 1},
                                edge_checks=[EdgeCheck("empty ok", True), EdgeCheck("unicode ok", False)],
                                robustness_checks=[EdgeCheck("400 on bad input", False, critical=True)])
        assert r.metrics["edge_cases"] == 50
        assert r.metrics["robustness"] == 0
        assert not r.passed                          # critical robustness failure

    def test_nested_and_list_comparison(self):
        gt  = {"rows": [{"n": "A", "d": "Lead"}, {"n": "B", "d": "PM"}]}
        gen = {"rows": [{"n": "A", "d": "Lead"}]}
        r = self.agent.evaluate(gen, gt)
        assert any("list length" in f.message for f in r.findings)

    def test_report_serialisable(self):
        r = self.agent.evaluate({"a": 1}, {"a": 2})
        json.dumps(r.to_dict())                      # must not raise

    def test_similarity_bounds(self):
        assert text_similarity("", "") == 1.0
        assert text_similarity("abc", "") == 0.0
        assert 0.0 <= text_similarity("power grid", "grid power") <= 1.0


# ═════════════════════════════════════════════════════════════════════════════
# Source-document provenance
# ═════════════════════════════════════════════════════════════════════════════

AEML_DOC = SourceDoc(
    name="AEML_Demand_Response.docx",
    path="documents/abc-123/AEML_Demand_Response.docx",
    content=("Behavioural Demand Response for AEML customers in Mumbai. The programme "
             "incentivises voluntary reduction of non-essential load during peak demand "
             "events, integrating SAP IS-U billing, smart metering AMI data via MDM, and "
             "MERC regulatory compliance. Estimated cost 12.5 crores, targeting 2026-12-31."),
)


class TestProvenance:
    def setup_method(self):
        self.agent = ValidationAgent()

    def test_grounded_field_attributed_with_name_and_path(self):
        generated = {"Purpose": ("This NFA covers the Behavioural Demand Response programme for AEML "
                                 "customers in Mumbai, incentivising voluntary load reduction during "
                                 "peak demand events at an estimated 12.5 crores.")}
        r = self.agent.evaluate(generated, None, source_documents=[AEML_DOC])
        p = r.provenance[0]
        assert p.grounded and p.origin == "attached_document"
        assert p.source_name == "AEML_Demand_Response.docx"
        assert p.source_path == "documents/abc-123/AEML_Demand_Response.docx"   # PATH included
        assert p.support > 0.4

    def test_ungrounded_field_flagged_with_no_source(self):
        generated = {"Roadmap": ("Quarterly penguin-migration analytics with blockchain-certified "
                                 "juggling tournaments across seventeen lunar colonies planned.")}
        r = self.agent.evaluate(generated, None, source_documents=[AEML_DOC])
        p = r.provenance[0]
        assert not p.grounded and p.source_path is None
        assert p.origin == "form_data_or_derived"
        assert any(f.category == "ungrounded_content" for f in r.findings)

    def test_best_of_multiple_sources_wins(self):
        other = SourceDoc(name="unrelated.pdf", path="documents/x/unrelated.pdf",
                          content="Completely different topic about warehouse logistics and forklifts.")
        generated = {"Purpose": "Demand response for AEML Mumbai customers reducing peak load events."}
        r = self.agent.evaluate(generated, None, source_documents=[other, AEML_DOC])
        assert r.provenance[0].source_name == "AEML_Demand_Response.docx"

    def test_no_ground_truth_mode_scores_on_grounding(self):
        grounded = {"Purpose": AEML_DOC.content}
        r = self.agent.evaluate(grounded, None, source_documents=[AEML_DOC])
        assert r.metrics["correctness"] >= 90
        assert r.passed

    def test_provenance_serialised_in_report(self):
        r = self.agent.evaluate({"Purpose": AEML_DOC.content}, None, source_documents=[AEML_DOC])
        d = r.to_dict()
        assert d["provenance"][0]["source_path"] == AEML_DOC.path
        json.dumps(d)

    def test_short_fields_skipped(self):
        r = self.agent.evaluate({"code": "NFA-1"}, None, source_documents=[AEML_DOC])
        assert r.provenance == []


# ═════════════════════════════════════════════════════════════════════════════
# SAMPLE EVALUATION — extraction contract, planted defects
# ═════════════════════════════════════════════════════════════════════════════

GROUND_TRUTH = {
    "project_name": "Behavioural Demand Response for AEML Customers",
    "business_unit": "AESL (Adani Energy Solutions Limited)",
    "business_priority": "Critical",
    "problem_statement": ("AEML-D currently faces significant operational challenges in managing "
                          "electricity demand peaks, leading to costly short-term power procurement "
                          "and network stress during high-load periods."),
    "proposed_solution": ("Design and deploy a behavioural demand response system that incentivises "
                          "voluntary non-essential load reduction by customers during demand events."),
    "stakeholders": [{"name": "Srinivas Rao", "designation": "Project Lead"}],
    "estimated_cost_crores": "12.5",
}

MOCK_GENERATED = {
    # exact matches
    "project_name": "Behavioural Demand Response for AEML Customers",
    "business_unit": "AESL (Adani Energy Solutions Limited)",
    # planted defect 1 — wrong value
    "business_priority": "Non-Critical",
    # semantically equivalent paraphrase (must NOT be flagged wrong)
    "problem_statement": ("Managing peaks in electricity demand is a major operational challenge for "
                          "AEML-D today — high-load periods force costly short-term power purchases "
                          "and put stress on the network."),
    "proposed_solution": ("Build and roll out a behavioural demand response programme that rewards "
                          "customers for voluntarily reducing non-essential load during demand events."),
    "stakeholders": [{"name": "Srinivas Rao", "designation": "Project Lead"}],
    # planted defect 2 — required field missing:  estimated_cost_crores absent
    # planted defect 3 — extra field not in spec
    "hallucinated_field": "should be flagged as extra",
}

REQUIRED = ["project_name", "business_unit", "problem_statement", "proposed_solution"]


class TestSampleEvaluation:
    def test_reference_evaluation(self, capsys):
        agent = ValidationAgent()
        report = agent.evaluate(
            MOCK_GENERATED, GROUND_TRUTH,
            required_fields=REQUIRED,
            exact_fields=["project_code"],
            edge_checks=[
                EdgeCheck("unicode preserved (₹ / Devanagari fields)", True),
                EdgeCheck("empty stakeholder rows filtered", True),
                EdgeCheck("null fields returned as null not omitted", True),
            ],
            robustness_checks=[
                EdgeCheck("malformed body → 400 {'error'}", True),
                EdgeCheck("unsupported file ext → 415", True),
                EdgeCheck("duplicate project_code → 409 with conflict id", True),
            ],
        )

        with capsys.disabled():
            # plain ASCII — Windows cp1252 consoles choke on box-drawing chars
            print("\n" + "=" * 66)
            print(" SAMPLE EVALUATION -- mock generated output vs ground truth")
            print("=" * 66)
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=True)[:2400])

        # The agent must catch every planted defect:
        cats = [(f.category, f.path) for f in report.findings]
        assert ("incorrect_value", "business_priority") in cats          # defect 1
        assert ("missing_field", "estimated_cost_crores") in cats        # defect 2
        assert ("extra_field", "hallucinated_field") in cats             # defect 3
        # …and must NOT flag the valid paraphrases:
        assert not any(f.path == "problem_statement" and f.category == "incorrect_value"
                       for f in report.findings)
        # Score lands where a mostly-correct output should: above bar, imperfect.
        assert PASS_THRESHOLD <= report.score < 100
        assert report.passed
