"""Load test against a running API: latency percentiles, throughput, errors, server metrics.

    uvicorn app.main:app --port 8000 &
    python scripts/load_test.py --base-url http://127.0.0.1:8000 --requests 2000 --concurrency 1 4 16

Requests cycle through the 30 evaluation questions (with their as-of dates), so the
mix includes answers, abstentions and refused writes. For each concurrency level it
reports client-side p50/p95/p99 latency, throughput and non-2xx responses, then
reads /metrics and reports the server's own counts: outcomes, abstention reasons
and verification-gate failures by check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def parse_metrics(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r'^(agent_runs_total|agent_abstentions_total|gate_check_failures_total)\{(.*)\} ([0-9.e+]+)$', line)
        if m:
            out[f"{m.group(1)}{{{m.group(2)}}}"] = float(m.group(3))
    return out


async def run_level(base: str, payloads: list[dict], n: int, concurrency: int) -> dict:
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    counter = iter(range(n))
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        async def worker() -> None:
            for i in counter:
                body = payloads[i % len(payloads)]
                t = time.perf_counter()
                r = await client.post("/ask", json=body)
                latencies.append((time.perf_counter() - t) * 1000)
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1

        start = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.perf_counter() - start
    return {"concurrency": concurrency, "requests": n, "wall_s": round(wall, 2),
            "throughput_rps": round(n / wall, 1),
            "latency_ms": {"p50": round(percentile(latencies, 0.50), 2), "p95": round(percentile(latencies, 0.95), 2),
                           "p99": round(percentile(latencies, 0.99), 2), "mean": round(statistics.mean(latencies), 2)},
            "non_2xx": sum(v for k, v in statuses.items() if not 200 <= k < 300)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 16])
    ap.add_argument("--architecture", choices=["single", "multi"], default="single")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=None, help="write results as JSON to this path")
    args = ap.parse_args()
    cases = [json.loads(line) for line in (ROOT / "evaluations/cases.jsonl").read_text().splitlines() if line]
    payloads = [{"question": c["question"], "as_of": c.get("as_of"), "include_trace": False,
                 "architecture": args.architecture} for c in cases]
    health = httpx.get(f"{args.base_url}/health").json()
    before = parse_metrics(httpx.get(f"{args.base_url}/metrics").text)
    levels = [asyncio.run(run_level(args.base_url, payloads, args.requests, c)) for c in args.concurrency]
    after = parse_metrics(httpx.get(f"{args.base_url}/metrics").text)
    server = {k: after[k] - before.get(k, 0.0) for k in after if after[k] - before.get(k, 0.0) > 0}
    res = {"label": args.label, "architecture": args.architecture, "database": health["database"],
           "levels": levels, "server_metrics_delta": server}
    print(json.dumps(res, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=2) + "\n")


if __name__ == "__main__":
    main()
