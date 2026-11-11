# Model risk management: controls mapped to SR 11-7 and the NIST AI RMF

This document records the claim-audit agent the way a model inventory entry would, and
maps each control in the repository to the two frameworks financial institutions most
often use: the Federal Reserve / OCC guidance on model risk management (SR 11-7,
OCC 2011-12) and the NIST AI Risk Management Framework (AI RMF 1.0, with its Generative
AI Profile, NIST AI 600-1). It is a mapping of what the code does, with a link to the
evidence for each line; it is not a certification or a validation report.

## 1. Model inventory entry

| Field | Value |
|---|---|
| Name | Financial claim-audit agent |
| Purpose | Answer questions about reported company figures (original, current, as of a date, restatement history) with citations to the filing that published each number |
| Users | Analysts and internal tools through `/ask`; downstream systems through the read API and the `claims.ingested` stream |
| Model components | Planner (deterministic `RulePlanner`, or an LLM through an OpenAI-compatible endpoint); optional four-role LangGraph pipeline (retriever, analyst, writer, verifier) |
| Non-model components that bound it | Read-only tool registry, six-check verification gate (`app/verify.py`), answer rendering from the claim store (`answer_mode=rendered`) |
| Data | Versioned filings and claims (append-only); synthetic fixtures in this repository |
| Materiality / tier (proposed) | Tier 2: informs decisions, does not execute them; every released figure is traceable to a source filing |
| Known limitations | See section 5 |

## 2. SR 11-7: the three pillars

### 2.1 Development, implementation and use

| Expectation | Control | Evidence |
|---|---|---|
| Sound design, documented purpose and limits | README architecture, threat table and status table; this document | `README.md` |
| Data quality and suitability | Validation at ingestion: unknown metric or unit, unit/metric mismatch, sign rules, missing provenance, duplicate facts, amendment ordering; a rejected document writes nothing | `app/ingest.py`, `tests/test_ingest_validation.py`, `fixtures/invalid/` |
| Data lineage | Immutable filings with SHA-256; append-only claims; every answer cites accession, form and filing date; S3 object versions and Kafka offsets recorded as source URIs; knowledge-graph lineage queries | `app/database.py`, `app/graph.py`, `/graph/lineage/{claim_id}`, `tests/test_graph.py` |
| Testing during development | 100+ unit, integration and evaluation tests on SQLite and PostgreSQL in CI | `tests/`, `.github/workflows/tests.yml` |
| Use within intended scope | Out-of-scope and write requests are refused; the database connection used by the agent is read-only at the database level | `tests/test_agent_safety.py::test_write_requests_are_refused_and_db_unchanged`, `::test_agent_connection_is_read_only_at_database_level` |

### 2.2 Validation

| Element | Control | Evidence |
|---|---|---|
| Conceptual soundness | The model only proposes; numbers are checked against the claim store before release, or written from it (rendered mode) | `app/agent.py`, `app/verify.py` |
| Outcomes analysis | Fixed 30-case evaluation scored on value, unit, source, filing date, abstention and look-ahead separately | `evaluations/run_eval.py`, `evaluations/results_rule.md` |
| Benchmarking / challenger | Single agent vs multi-agent pipeline on accuracy, containment and fault recovery under identical attack policies | `evaluations/multi_agent.py`, `evaluations/results_multi_agent.md` |
| Sensitivity and stress testing | Red-team suite (30 attacks, 10 classes) from a planner that has fully followed the injected instruction; ablation of each gate check; injected role faults at 10% and 30% rates | `evaluations/red_team.py`, `tests/test_red_team.py`, `tests/test_multi_agent_eval.py` |
| Effective challenge of limits | False-block measurement on a held-out set of honest answers written after the fixes; the one attack free-text checks cannot stop is kept as a strict `xfail` | `evaluations/results_red_team.md`, `tests/test_agent_safety.py` |
| Ongoing monitoring | Prometheus metrics and alert rules (abstention rate, gate failures by check, unregistered tool calls, tool errors, retries, latency, dead-lettered filings); CloudWatch alarms on AWS | `app/metrics.py`, `deploy/prometheus/alerts.yml`, `deploy/terraform/aws/monitoring.tf`, `tests/test_alerts.py` |

### 2.3 Governance, policies and controls

| Expectation | Control | Evidence |
|---|---|---|
| Change control | Every change runs lint, the full test suite on two databases, the evaluation, Terraform validation, the container smoke test and a Kubernetes install before merge | `.github/workflows/tests.yml` |
| Separation of duties in the system | Least-privilege IAM role per component; the API task role has no AWS permissions; the agent has no write tools | `deploy/terraform/aws/iam.tf`, `app/tools.py` |
| Secrets | Database password managed and rotated by RDS in Secrets Manager; LLM keys only from a secret store | `deploy/terraform/aws/data.tf`, `deploy/helm/claim-audit/values.yaml` |
| Auditability | Per-run trace (tool calls, arguments, results, verification outcome) returned with `trace_id` and logged as one JSON line; request IDs on every response | `app/agent.py`, `app/main.py` |

## 3. NIST AI RMF 1.0

| Function | Category (examples) | How it is met here |
|---|---|---|
| **Govern** | GOVERN 1 (policies), GOVERN 4 (documentation and accountability) | This inventory entry, README limits and status, CI as the enforced release gate |
| **Map** | MAP 2 (intended use and limits), MAP 4 (third-party components), MAP 5 (impacts) | Purpose and users above; LLM endpoint treated as untrusted (scripted misbehaving endpoints in tests); answers inform, never execute |
| **Measure** | MEASURE 2.3 (performance), 2.5 (validity and reliability), 2.7 (security and resilience), 2.8 (transparency), 2.11 (fairness and bias) | Fixed evaluation; red-team and ablation; fault-injection recovery; citations on every answer; bias is out of scope for a numeric look-up system and is not claimed |
| **Manage** | MANAGE 2 (risk treatment), MANAGE 3 (third-party risk), MANAGE 4 (monitoring and response) | Rendered answers + full gate as the configuration for figures; text-blind tools as the strongest containment; alerts that page on unregistered tool calls; dead-letter topic and S3 quarantine for rejected inputs |

Generative AI Profile (NIST AI 600-1) risks addressed:

| Risk | Control |
|---|---|
| Confabulation | Amount, role and currency checks; rendered answers never let the model write a number |
| Information integrity | Citations must be a subset of retrieved evidence; superseded figures cannot be presented as current |
| Information security (prompt injection) | Filing text reaches the model only as a labelled data field; multi-agent trust boundary keeps it from the writer; text-blind tools contain all 16 planted injections |
| Value chain and component integration | LLM backends behind one interface, each tested against scripted endpoints that misbehave |

## 4. Monitoring thresholds and owners (proposed)

| Signal | Threshold | Action |
|---|---|---|
| Unregistered tool called | any | Page; inspect the trace and the source document |
| Abstention rate (excluding refused writes) | > 50% for 10 min | Ticket; check for poisoned documents or a data gap |
| Gate failures by check | > 0.05/s for 5 min | Ticket; the check name points at the cause |
| Filings dead-lettered / quarantined | any | Ticket; review `filings.rejected` or `rejected/` |
| p95 latency | > 500 ms in-service, > 1 s at the load balancer | Ticket; scale out (one worker per task) |

## 5. Known limitations and residual risk

* All evaluation data is synthetic and was written by the same author as the planner and
  the attacks; results measure the controls, not generalisation to real filings.
* How often a hosted LLM follows an injection has not been measured; the red-team planner
  is a worst case that always follows it.
* One attack class (negated role swap) passes free-text verification; it is stopped only
  by rendered answers, which is why rendered mode is the configuration for figures.
* As-of resolution is by day; intraday filing times are not modelled.
