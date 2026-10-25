# Load test

`python scripts/load_test.py --requests 2000 --concurrency 1 4 16` against `uvicorn app.main:app` (one worker), deterministic roles, requests cycling through the 30 evaluation questions (answers, abstentions and refused writes). Server, database and load generator share one 2-core machine, so these are lower bounds for a real deployment.

| Database | Architecture | Concurrency | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Non-2xx |
|---|---|---|---|---|---|---|---|
| postgresql | single | 1 | 250.4 | 4.01 | 5.07 | 5.99 | 0 |
| postgresql | single | 4 | 311.6 | 12.53 | 19.11 | 22.04 | 0 |
| postgresql | single | 16 | 351.0 | 41.81 | 81.33 | 111.86 | 0 |
| postgresql | multi | 1 | 179.0 | 5.99 | 6.96 | 7.48 | 0 |
| postgresql | multi | 4 | 197.2 | 20.48 | 31.02 | 36.4 | 0 |
| postgresql | multi | 16 | 185.2 | 84.06 | 138.52 | 171.86 | 0 |
| sqlite | single | 1 | 334.5 | 2.98 | 3.64 | 4.25 | 0 |
| sqlite | single | 4 | 395.5 | 9.52 | 16.95 | 21.18 | 0 |
| sqlite | single | 16 | 370.1 | 41.41 | 68.63 | 82.25 | 0 |

Server-side counters read from `/metrics` over the PostgreSQL runs (6,000 requests per architecture):

| Architecture | Answered | Abstained: no evidence | Abstained: out of scope | Refused writes |
|---|---|---|---|---|
| single | 4008 | 801 | 795 | 396 |
| multi | 4008 | 801 | 795 | 396 |

The counters match the request mix exactly: each 2,000-request level is 66 passes over the 30 questions plus the first 20 of them, which gives 66 × 20 + 16 = 1,336 answerable requests per level and 4,008 over three levels.
