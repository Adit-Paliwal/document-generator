"""
Load test — IntelliDraft FastAPI server.
Async httpx, N concurrent workers per scenario, latency percentiles + error counts.

Usage:  python tests/load_test.py http://127.0.0.1:7073 [requests_per_scenario] [concurrency]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

import httpx

# Defaults — overridden by CLI args in __main__ (kept import-safe: pytest
# collects this file because of the *_test.py pattern; argv must not be
# parsed at import time).
BASE = "http://127.0.0.1:7073"
N    = 200
C    = 20

HDRS = {"X-User-Email": "author@test.com", "X-User-Name": "Load Tester"}

SCENARIOS = [
    ("health (no DB)",        "GET", "/api/health"),
    ("projects list+rollup",  "GET", "/api/projects?per_page=50"),
    ("projects stats (KPI)",  "GET", "/api/projects/stats"),
    ("notifications",         "GET", "/api/notifications?limit=50"),
    ("review sent list",      "GET", "/api/review/sent"),
    ("templates",             "GET", "/api/templates"),
    ("SPA index",             "GET", "/"),
]


async def run_scenario(name, method, path):
    lat, errors, statuses = [], 0, {}
    sem = asyncio.Semaphore(C)
    async with httpx.AsyncClient(timeout=30) as client:
        async def one():
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await client.request(method, BASE + path, headers=HDRS)
                    statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
                    if r.status_code >= 500:
                        errors += 1
                except Exception:
                    errors += 1
                    statuses["EXC"] = statuses.get("EXC", 0) + 1
                lat.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(N)])
        wall = time.perf_counter() - t0

    lat.sort()
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]
    print(f"{name:24s}  n={N} c={C}  rps={N / wall:7.1f}  "
          f"p50={statistics.median(lat):7.1f}ms  p95={pct(.95):7.1f}ms  p99={pct(.99):7.1f}ms  "
          f"max={lat[-1]:7.1f}ms  5xx/exc={errors}  statuses={statuses}")


async def main():
    print(f"Load test -> {BASE}  ({N} req/scenario, concurrency {C})\n")
    for s in SCENARIOS:
        await run_scenario(*s)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        BASE = sys.argv[1].rstrip("/")
    if len(sys.argv) > 2:
        N = int(sys.argv[2])
    if len(sys.argv) > 3:
        C = int(sys.argv[3])
    asyncio.run(main())
