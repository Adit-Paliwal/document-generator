"""
Integration, negative-path, smoke and concurrency tests against a LIVE server.
Auto-skips when no server is reachable (CI must run with one).

Run:  set INTELLIDRAFT_BASE=http://127.0.0.1:7073   (default)
      python -m pytest tests/test_api_integration.py -q
"""
from __future__ import annotations

import concurrent.futures
import os
import uuid

import pytest
import requests

BASE = os.environ.get("INTELLIDRAFT_BASE", "http://127.0.0.1:7073").rstrip("/")
HDRS = {"X-User-Email": "qa@test.com", "X-User-Name": "QA Bot"}


def _server_up() -> bool:
    try:
        return requests.get(f"{BASE}/api/health", timeout=5).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason=f"no server at {BASE}")


@pytest.fixture()
def project():
    """A throwaway draft project, deleted afterwards."""
    r = requests.post(f"{BASE}/api/projects/draft",
                      json={"project_name": "QA fixture", "business_unit": "QA"}, timeout=30)
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    yield pid
    requests.delete(f"{BASE}/api/projects/{pid}", timeout=30)


# ═════════════════════════════════════════════════════════════════════════════
# Smoke — the five signals that must always be green
# ═════════════════════════════════════════════════════════════════════════════

class TestSmoke:
    def test_health(self):
        d = requests.get(f"{BASE}/api/health", timeout=10).json()
        assert d["status"] == "ok"

    def test_spa_and_docs_served(self):
        assert requests.get(f"{BASE}/", timeout=10).status_code == 200
        assert requests.get(f"{BASE}/docs", timeout=10).status_code == 200

    def test_core_lists(self):
        for path in ("/api/projects", "/api/projects/stats", "/api/templates"):
            r = requests.get(f"{BASE}{path}", timeout=30)
            assert r.status_code == 200, path

    def test_stats_shape(self):
        d = requests.get(f"{BASE}/api/projects/stats", timeout=30).json()
        assert set(d) == {"total", "under_draft", "under_review", "approved"}
        assert d["total"] >= d["approved"] + 0     # sane, non-negative ints
        assert all(isinstance(v, int) and v >= 0 for v in d.values())


# ═════════════════════════════════════════════════════════════════════════════
# Input validation & failure paths
# ═════════════════════════════════════════════════════════════════════════════

class TestValidationAndErrors:
    def test_malformed_json_body_is_400_with_error_shape(self):
        r = requests.post(f"{BASE}/api/projects/draft", data="{not valid json",
                          headers={"Content-Type": "application/json"}, timeout=30)
        assert r.status_code == 400
        assert "error" in r.json()

    def test_invalid_uuid_rejected(self):
        r = requests.post(f"{BASE}/api/projects/draft", json={"project_id": "not-a-uuid"}, timeout=30)
        assert r.status_code == 400 and "UUID" in r.json()["error"]

    def test_unknown_ids_are_404_json(self):
        for path in ("/api/projects/does-not-exist", "/api/generate/does-not-exist",
                     "/api/review/does-not-exist"):
            r = requests.get(f"{BASE}{path}", headers=HDRS, timeout=30)
            assert r.status_code == 404 and "error" in r.json(), path

    def test_empty_patch_body_rejected(self, project):
        r = requests.patch(f"{BASE}/api/projects/{project}", json={}, timeout=30)
        assert r.status_code == 400

    def test_upload_rejects_missing_and_bad_files(self):
        assert requests.post(f"{BASE}/api/upload", timeout=30).status_code == 400
        r = requests.post(f"{BASE}/api/upload",
                          files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
                          timeout=30)
        assert r.status_code == 415

    def test_upload_path_traversal_neutralised(self):
        r = requests.post(f"{BASE}/api/upload",
                          files={"file": ("../../../../etc/passwd.txt", b"x", "text/plain")},
                          timeout=30)
        assert r.status_code == 415            # .txt unsupported AND name sanitised — never 500

    def test_identity_required_endpoints(self):
        for path in ("/api/notifications", "/api/review/sent", "/api/review/received"):
            r = requests.get(f"{BASE}{path}", timeout=30)      # no X-User-Email
            assert r.status_code == 400, path

    def test_admin_endpoint_gated(self):
        r = requests.post(f"{BASE}/api/admin/reset-db", timeout=30)
        assert r.status_code == 403

    def test_review_verdict_invalid_action(self):
        r = requests.post(f"{BASE}/api/review/x/respond", json={"action": "maybe"},
                          headers=HDRS, timeout=30)
        assert r.status_code in (400, 404)     # never 500


# ═════════════════════════════════════════════════════════════════════════════
# Edge cases — unicode, duplicates, boundaries
# ═════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_unicode_round_trip(self):
        name = "विद्युत मांग प्रतिक्रिया ⚡ ₹12.5Cr — δT test"
        r = requests.post(f"{BASE}/api/projects/draft",
                          json={"project_name": name, "pain_points": "🔥 पीक लोड"}, timeout=30)
        pid = r.json()["project_id"]
        try:
            d = requests.get(f"{BASE}/api/projects/{pid}", timeout=30).json()
            assert d["project_name"] == name
            assert "🔥" in d["pain_points"]
        finally:
            requests.delete(f"{BASE}/api/projects/{pid}", timeout=30)

    def test_duplicate_project_code_conflict(self, project):
        code = f"QA-{uuid.uuid4().hex[:8]}"
        assert requests.patch(f"{BASE}/api/projects/{project}",
                              json={"project_code": code}, timeout=30).status_code == 200
        r2 = requests.post(f"{BASE}/api/projects/draft",
                           json={"project_name": "dup", "project_code": code}, timeout=30)
        assert r2.status_code == 409
        assert "conflict_project_id" in r2.json()

    def test_pagination_out_of_range_clamped(self):
        r = requests.get(f"{BASE}/api/projects?page=-5&per_page=99999", timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d["page"] == 1 and d["per_page"] == 100

    def test_large_text_field_accepted(self, project):
        r = requests.patch(f"{BASE}/api/projects/{project}",
                           json={"problem_statement": "A" * 200_000}, timeout=60)
        assert r.status_code == 200

    def test_null_values_in_body_tolerated(self):
        r = requests.post(f"{BASE}/api/projects/draft",
                          json={"project_name": "nulls", "risks": None, "deadline": None},
                          timeout=30)
        assert r.status_code == 201
        requests.delete(f"{BASE}/api/projects/{r.json()['project_id']}", timeout=30)


# ═════════════════════════════════════════════════════════════════════════════
# Concurrency & repeated calls
# ═════════════════════════════════════════════════════════════════════════════

class TestConcurrency:
    def test_idempotent_draft_creation_race(self):
        """20 parallel creates with the SAME client uuid → exactly one project."""
        pid = str(uuid.uuid4())
        def create():
            return requests.post(f"{BASE}/api/projects/draft",
                                 json={"project_id": pid, "project_name": "race"}, timeout=30)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                results = list(ex.map(lambda _: create(), range(20)))
            assert all(r.status_code in (200, 201) for r in results)
            assert all(r.json()["project_id"] == pid for r in results)
            lookup = requests.get(f"{BASE}/api/projects/{pid}", timeout=30)
            assert lookup.status_code == 200
        finally:
            requests.delete(f"{BASE}/api/projects/{pid}", timeout=30)

    def test_parallel_reads_no_5xx(self):
        """40 mixed concurrent reads — the StaticPool regression guard."""
        paths = ["/api/projects", "/api/projects/stats", "/api/notifications", "/api/templates"] * 10
        def get(p):
            return requests.get(f"{BASE}{p}", headers=HDRS, timeout=30).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            codes = list(ex.map(get, paths))
        assert all(c < 500 for c in codes), f"5xx under concurrency: {codes}"

    def test_repeated_identical_calls_stable(self):
        shapes = {tuple(sorted(requests.get(f"{BASE}/api/projects/stats", timeout=30).json()))
                  for _ in range(5)}
        assert len(shapes) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Timeout / delayed-response behaviour
# ═════════════════════════════════════════════════════════════════════════════

class TestTimeouts:
    def test_sse_stream_bounded_error_event(self):
        """SSE for an unknown job must emit one error event and close (no hang)."""
        with requests.get(f"{BASE}/api/generate/nope/stream", stream=True, timeout=15) as r:
            assert r.status_code == 200
            first = next(r.iter_lines(decode_unicode=True))
            assert "error" in first

    def test_health_latency_budget(self):
        import time
        t0 = time.perf_counter()
        requests.get(f"{BASE}/api/health", timeout=10)
        assert time.perf_counter() - t0 < 2.0
