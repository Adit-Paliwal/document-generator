# IntelliDraft — Test Plan

**Owner:** QA / test-automation. **Runtime:** Python 3.11+, pytest 9.
**Quality bar:** overall Validation-Agent score ≥ 80, all CRITICAL tests pass,
no unhandled edge case in the matrix below, error handling per spec
(`{"error": ...}` + correct HTTP status).

## 1. Scope — systems under test

| Module | Surface | Criticality |
|---|---|---|
| `generation/ontology.py` | doc-type normalisation, selective glossary/tech matching, 4 prompt assemblies | CRITICAL (feeds every LLM call) |
| `generation/review_service.py` | `_validate_persona_review`, `_extract_json`, `_comments_fingerprint`, notification emission | CRITICAL |
| `generation/generator.py` | `_build_system_prompt` (template integrity, truncation), `_estimate_max_tokens` | CRITICAL |
| `generation/generation_service.py` | `project_review_rollup`, wave-parallel invariants (atomic counter) | HIGH |
| FastAPI surface (62 routes) | contract (status + shape), auth-ish guards, validation errors | CRITICAL |
| Validation Agent (`tests/validation_agent.py`) | scoring engine itself | HIGH |

## 2. Test types & where they live

| Type | File | Needs server? | Needs LLM? |
|---|---|---|---|
| Unit + edge cases | `test_unit.py` | no | no |
| Validation Agent unit + sample evaluation | `test_validation_agent.py` | no | no (LLM judge optional) |
| Integration + negative-path API | `test_api_integration.py` | yes (`INTELLIDRAFT_BASE`, default `http://127.0.0.1:7073`) | no |
| Regression (API contract, 62 steps) | `api_contract.py --compare` | yes | no |
| Smoke | `test_api_integration.py::TestSmoke` | yes | no |
| Performance sanity | `test_unit.py::TestPerformanceSanity` + `load_test.py` | partial | no |

Run: `cd Data_Ingestion && ..\env\Scripts\python.exe -m pytest tests -q`
(integration tests auto-skip when the server is down; CI must run them).

## 3. Edge-case matrix (explicit coverage)

| Edge case | Covered by |
|---|---|
| Empty inputs | ontology empty scan, `_extract_json("")`, rollup([]), API empty-body PATCH → 400 |
| Null / None | `_validate_persona_review(None)`, doc-type None, JSON `null` fields on draft create |
| Extremely large inputs | 1 MB glossary scan (perf-capped), >50 MB upload → 413, 8 000-char llm_context truncation |
| Unicode / special chars | ΔT glossary key, Devanagari + emoji project names round-trip, `₹` in fields |
| Duplicate values | duplicate `project_code` → 409, duplicate reviewer emails deduped on share |
| Out-of-range numbers | `page=-5`, `per_page=99999` clamped; `accept version 999` → 404-path |
| Invalid formats | non-UUID `project_id` → 400, malformed JSON body → 400 (not 422/500), bad file ext → 415 |
| Concurrency / repeated calls | 20 parallel draft creations w/ same client UUID (idempotent), parallel notifications reads (no 5xx) |
| Timeout / delayed responses | tenacity retry unit-verified transient-only; SSE bounded read |
| Partial / corrupted payloads | truncated JSON to `_extract_json`, persona review missing keys → retry contract |

## 4. Validation Agent

`tests/validation_agent.py` — compares generated output vs ground truth:
structural diff (missing / extra / incorrect fields, type mismatches; key order
and whitespace ignored), semantic text equivalence (token-F1 + difflib blend;
optional Gemini judge via `use_llm=True`), error-handling grading.
Weighted score: correctness 40 · completeness 20 · format 15 · edge cases 15 ·
robustness 10. Verdict: PASS iff score ≥ 80 **and** no CRITICAL finding.

## 5. Exit criteria

1. `pytest tests -q` green (0 failures; skips only for down server).
2. `api_contract.py --compare` → 62/62.
3. Validation Agent sample evaluation ≥ 80 with correct finding detection
   (must flag the planted missing field, wrong value, and extra field).
4. Load sanity: 0 × 5xx at c=20 on hot endpoints.
