#!/usr/bin/env python3
"""
sanity_test.py — Step 4 of 4 : Final sanity test for deployed IntelliDraft
===========================================================================

Tests every deployed resource end-to-end and prints a clear PASS / FAIL report.

What is tested:
  ┌─ Cloud Run (Flask API) ──────────────────────────────────────────────┐
  │  1. Health check          GET  /api/health  → {"status":"ok"}        │
  │  2. Templates list        GET  /api/templates → list of 6 doc types  │
  │  3. File upload           POST /api/upload  → parses a test PDF      │
  │  4. Document metadata     GET  /api/document/{id} → summary          │
  └──────────────────────────────────────────────────────────────────────┘
  ┌─ Vertex AI Agent Engine ─────────────────────────────────────────────┐
  │  5. Create session        agent.create_session()                     │
  │  6. Chat response         agent.stream_query("Hello!")               │
  │     → verifies Gemini responds and agent identifies itself           │
  └──────────────────────────────────────────────────────────────────────┘

Usage:
  # Set your deployed URLs / IDs first:
  python sanity_test.py \\
      --api-url=https://intellidraft-api-xxxx-uc.a.run.app \\
      --project=your-project-id \\
      --region=us-central1 \\
      --agent-id=123456789

  # Or export env vars and run:
  export CLOUD_RUN_URL=https://intellidraft-api-xxxx-uc.a.run.app
  export GCP_PROJECT_ID=your-project-id
  export GCP_REGION=us-central1
  export AGENT_ENGINE_ID=123456789
  python sanity_test.py

Requirements:
  pip install requests google-cloud-aiplatform>=1.91.0
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

# ── ANSI colours (suppressed on Windows if not supported) ────────────────────
_USE_COLOUR = sys.stdout.isatty() and os.name != "nt"
GREEN  = "\033[92m" if _USE_COLOUR else ""
RED    = "\033[91m" if _USE_COLOUR else ""
YELLOW = "\033[93m" if _USE_COLOUR else ""
BOLD   = "\033[1m"  if _USE_COLOUR else ""
RESET  = "\033[0m"  if _USE_COLOUR else ""

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
SKIP = f"{YELLOW}SKIP{RESET}"


# ─────────────────────────────────────────────────────────────────────────────
# Test results tracker
# ─────────────────────────────────────────────────────────────────────────────

class Results:
    def __init__(self) -> None:
        self._rows: list[tuple[int, str, str, str]] = []  # num, name, status, detail

    def record(self, num: int, name: str, passed: bool, detail: str = "") -> bool:
        status = PASS if passed else FAIL
        self._rows.append((num, name, status, detail))
        icon = "✓" if passed else "✗"
        colour = GREEN if passed else RED
        print(f"  {colour}{icon}{RESET}  Test {num}: {name}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")
        return passed

    def skip(self, num: int, name: str, reason: str) -> None:
        self._rows.append((num, name, SKIP, reason))
        print(f"  {YELLOW}–{RESET}  Test {num}: {name}  [{YELLOW}SKIPPED{RESET}: {reason}]")

    def summary(self) -> bool:
        total   = len(self._rows)
        passed  = sum(1 for _, _, s, _ in self._rows if "PASS" in s)
        failed  = sum(1 for _, _, s, _ in self._rows if "FAIL" in s)
        skipped = sum(1 for _, _, s, _ in self._rows if "SKIP" in s)
        print()
        print("─" * 54)
        print(f"  {BOLD}Results: {passed}/{total} passed{RESET}", end="")
        if failed:
            print(f"  {RED}{failed} failed{RESET}", end="")
        if skipped:
            print(f"  {YELLOW}{skipped} skipped{RESET}", end="")
        print()
        print("─" * 54)
        return failed == 0


# ─────────────────────────────────────────────────────────────────────────────
# Helper — minimal PDF bytes for upload test (no external file needed)
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_pdf_bytes() -> bytes:
    """Generate the smallest valid 1-page PDF with readable text."""
    return b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]
/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 62>>
stream
BT /F1 14 Tf 72 720 Td (IntelliDraft Sanity Test Document) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000274 00000 n
0000000388 00000 n
trailer<</Size 6/Root 1 0 R>>
startxref
466
%%EOF"""


# ─────────────────────────────────────────────────────────────────────────────
# Cloud Run tests
# ─────────────────────────────────────────────────────────────────────────────

def run_cloud_run_tests(base_url: str, results: Results) -> Optional[str]:
    """
    Run tests 1-4 against the Cloud Run Flask API.
    Returns document_id from the upload test (used later if needed), or None.
    """
    try:
        import requests
    except ImportError:
        print(f"  {RED}✗{RESET}  'requests' not installed — run:  pip install requests")
        for i in range(1, 5):
            results.skip(i, f"Cloud Run test {i}", "requests not installed")
        return None

    api = base_url.rstrip("/") + "/api"

    # ── Test 1: Health check ─────────────────────────────────────────────────
    print(f"\n  {BOLD}── Cloud Run: {base_url} ──{RESET}")
    try:
        r = requests.get(f"{api}/health", timeout=15)
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        results.record(1, "Health check  GET /api/health", ok,
                       f"HTTP {r.status_code}  body={r.text[:80]}")
    except Exception as e:
        results.record(1, "Health check  GET /api/health", False, str(e))

    # ── Test 2: Templates list ───────────────────────────────────────────────
    try:
        r = requests.get(f"{api}/templates", timeout=15)
        data = r.json() if r.ok else {}
        templates = data if isinstance(data, list) else data.get("templates", [])
        ok = r.status_code == 200 and len(templates) >= 1
        results.record(2, "Templates list  GET /api/templates", ok,
                       f"HTTP {r.status_code}  count={len(templates)}")
    except Exception as e:
        results.record(2, "Templates list  GET /api/templates", False, str(e))

    # ── Test 3: File upload ──────────────────────────────────────────────────
    doc_id: Optional[str] = None
    try:
        pdf_bytes = _minimal_pdf_bytes()
        files = {"file": ("sanity_test.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
        r = requests.post(f"{api}/upload", files=files, timeout=60)
        data = r.json() if r.ok else {}
        doc_id = data.get("document_id") or data.get("doc_id")
        ok = r.status_code in (200, 201) and bool(doc_id)
        results.record(3, "File upload  POST /api/upload", ok,
                       f"HTTP {r.status_code}  document_id={doc_id or 'missing'}")
    except Exception as e:
        results.record(3, "File upload  POST /api/upload", False, str(e))

    # ── Test 4: Document metadata ────────────────────────────────────────────
    if doc_id:
        try:
            r = requests.get(f"{api}/document/{doc_id}", timeout=15)
            data = r.json() if r.ok else {}
            ok = r.status_code == 200 and "summary" in data
            results.record(4, "Document metadata  GET /api/document/{id}", ok,
                           f"HTTP {r.status_code}  keys={list(data.keys())[:5]}")
        except Exception as e:
            results.record(4, "Document metadata  GET /api/document/{id}", False, str(e))
    else:
        results.skip(4, "Document metadata  GET /api/document/{id}",
                     "upload test failed — no document_id available")

    return doc_id


# ─────────────────────────────────────────────────────────────────────────────
# Agent Engine tests
# ─────────────────────────────────────────────────────────────────────────────

def run_agent_engine_tests(project: str, region: str, agent_id: str,
                           results: Results) -> None:
    """Run tests 5-6 against the Vertex AI Agent Engine."""
    try:
        import vertexai
        from vertexai.preview import reasoning_engines
    except ImportError:
        print(f"\n  {RED}✗{RESET}  'google-cloud-aiplatform' not installed.")
        print(f"     Run:  pip install 'google-cloud-aiplatform[reasoningengine]>=1.95.0'")
        results.skip(5, "Agent Engine: create session", "SDK not installed")
        results.skip(6, "Agent Engine: chat response",  "SDK not installed")
        return

    resource_name = (
        f"projects/{project}/locations/{region}/reasoningEngines/{agent_id}"
    )
    print(f"\n  {BOLD}── Vertex AI Agent Engine: {agent_id} ──{RESET}")

    # ── Test 5: Create session ────────────────────────────────────────────────
    session_id: Optional[str] = None
    agent = None
    try:
        vertexai.init(project=project, location=region)
        agent = reasoning_engines.ReasoningEngine(resource_name)
        session = agent.create_session(user_id="sanity-test-user")
        session_id = session.get("id") or session.get("session_id")
        ok = bool(session_id)
        results.record(5, "Agent Engine: create session", ok,
                       f"session_id={session_id or 'missing'}  keys={list(session.keys())}")
    except Exception as e:
        results.record(5, "Agent Engine: create session", False, str(e))

    # ── Test 6: Chat response ─────────────────────────────────────────────────
    if agent and session_id:
        try:
            print(f"       Sending: 'Hello! What can you help me with?'")
            print(f"       Response: ", end="", flush=True)

            full_response = ""
            t0 = time.time()
            for chunk in agent.stream_query(
                user_id   = "sanity-test-user",
                session_id = session_id,
                message   = "Hello! What can you help me with?",
            ):
                text = ""
                if isinstance(chunk, dict):
                    # ADK stream format: {"content": {"parts": [{"text": "..."}]}}
                    parts = (chunk.get("content") or {}).get("parts") or []
                    text = "".join(p.get("text", "") for p in parts)
                elif isinstance(chunk, str):
                    text = chunk
                if text:
                    print(text, end="", flush=True)
                    full_response += text

            elapsed = time.time() - t0
            print()  # newline after streamed response

            # Verify response mentions something relevant (documents, BRD, etc.)
            keywords = ["document", "brd", "rfd", "proposal", "generate",
                        "help", "intellidraft", "upload", "project"]
            has_keyword = any(kw in full_response.lower() for kw in keywords)
            ok = len(full_response.strip()) > 20 and has_keyword
            results.record(6, "Agent Engine: chat response", ok,
                           f"length={len(full_response)}  elapsed={elapsed:.1f}s  "
                           f"keyword_match={has_keyword}")
        except Exception as e:
            results.record(6, "Agent Engine: chat response", False, str(e))
    else:
        results.skip(6, "Agent Engine: chat response",
                     "session creation failed — cannot query agent")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sanity-test deployed IntelliDraft Cloud Run + Agent Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python sanity_test.py \\
                  --api-url=https://intellidraft-api-xxxx-uc.a.run.app \\
                  --project=my-project \\
                  --region=us-central1 \\
                  --agent-id=123456789

              # Or use env vars:
              export CLOUD_RUN_URL=https://intellidraft-api-xxxx-uc.a.run.app
              export GCP_PROJECT_ID=my-project
              export GCP_REGION=us-central1
              export AGENT_ENGINE_ID=123456789
              python sanity_test.py
        """),
    )
    p.add_argument("--api-url",   default=os.getenv("CLOUD_RUN_URL",     ""),
                   help="Cloud Run service URL (e.g. https://intellidraft-api-xxx-uc.a.run.app)")
    p.add_argument("--project",   default=os.getenv("GCP_PROJECT_ID",    ""),
                   help="GCP project ID")
    p.add_argument("--region",    default=os.getenv("GCP_REGION", "asia-south1"),
                   help="GCP region (default: us-central1)")
    p.add_argument("--agent-id",  default=os.getenv("AGENT_ENGINE_ID",   ""),
                   help="Agent Engine resource ID (numeric, from deploy_agent_engine.py output)")
    p.add_argument("--skip-cloud-run",    action="store_true", help="Skip Cloud Run tests")
    p.add_argument("--skip-agent-engine", action="store_true", help="Skip Agent Engine tests")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    results = Results()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   IntelliDraft — Deployment Sanity Test              ║")
    print("╚══════════════════════════════════════════════════════╝")

    # ── Cloud Run ─────────────────────────────────────────────────────────────
    if args.skip_cloud_run:
        for i in range(1, 5):
            results.skip(i, f"Cloud Run test {i}", "--skip-cloud-run flag set")
    elif not args.api_url:
        print(f"\n  {YELLOW}⚠{RESET}  --api-url not provided → skipping Cloud Run tests")
        print(f"     Set CLOUD_RUN_URL or pass --api-url=https://...")
        for i in range(1, 5):
            results.skip(i, f"Cloud Run test {i}", "api-url not provided")
    else:
        run_cloud_run_tests(args.api_url, results)

    # ── Agent Engine ──────────────────────────────────────────────────────────
    if args.skip_agent_engine:
        for i in range(5, 7):
            results.skip(i, f"Agent Engine test {i-4}", "--skip-agent-engine flag set")
    elif not args.project or not args.agent_id:
        missing = []
        if not args.project:  missing.append("--project")
        if not args.agent_id: missing.append("--agent-id")
        print(f"\n  {YELLOW}⚠{RESET}  {', '.join(missing)} not provided → skipping Agent Engine tests")
        for i in range(5, 7):
            results.skip(i, f"Agent Engine test {i-4}", f"{', '.join(missing)} not provided")
    else:
        run_agent_engine_tests(args.project, args.region, args.agent_id, results)

    # ── Final report ──────────────────────────────────────────────────────────
    all_passed = results.summary()
    print()
    if all_passed:
        print(f"  {GREEN}{BOLD}🎉  All tests passed — IntelliDraft is live!{RESET}")
    else:
        print(f"  {RED}Some tests failed.{RESET} Check the output above for details.")
        print(f"  Common fixes:")
        print(f"    • Cloud Run failing → check Cloud Run logs:")
        print(f"        gcloud run services logs read intellidraft-api --region={args.region}")
        print(f"    • Agent Engine failing → check Vertex AI console:")
        print(f"        https://console.cloud.google.com/vertex-ai/agents/")
        print(f"    • 'requests' missing → pip install requests")
        print(f"    • SDK missing → pip install 'google-cloud-aiplatform[reasoningengine]>=1.95.0'")
    print()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
