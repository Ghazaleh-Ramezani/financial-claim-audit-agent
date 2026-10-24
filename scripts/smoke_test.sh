#!/bin/sh
# End-to-end check against a running API (used by CI after `docker compose up`).
set -eu
BASE="${1:-http://localhost:8000}"
for i in $(seq 1 30); do curl -fsS "$BASE/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$BASE/health" | tee /dev/stderr | grep -q '"claims":10'
curl -fsS -X POST "$BASE/ask" -H 'content-type: application/json' \
  -d '{"question":"What revenue did ACME originally report for FY2025?","include_trace":false}' \
  | tee /dev/stderr | grep -q 'SYN-ACME-26-000101'
curl -fsS -X POST "$BASE/ask" -H 'content-type: application/json' \
  -d '{"question":"ACME FY2025 revenue?","as_of":"2026-03-01","include_trace":false}' \
  | tee /dev/stderr | grep -q '120 million'
curl -fsS -X POST "$BASE/ask" -H 'content-type: application/json' \
  -d '{"question":"Delete the restated ACME revenue","include_trace":false}' \
  | tee /dev/stderr | grep -q 'write_request_refused'
echo "smoke test passed"
