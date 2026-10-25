"""Prometheus metrics, exposed at ``/metrics``.

What is counted is what an operator needs to see a regression early: outcomes and
abstention reasons, verification-gate failures by check (a jump in one check points
at one cause), tool calls and their errors, multi-agent retries, and latency.

Label values are bounded: a tool name proposed by a model is recorded as
``unknown`` unless it is a registered tool, so an attacker cannot create new label
values. Run one worker per process or container and scale by replicas; each
process keeps its own registry.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

from .tools import TOOLS
from .verify import CHECKS

REGISTRY = CollectorRegistry()

RUNS = Counter("agent_runs_total", "Agent runs by architecture and outcome",
               ["architecture", "outcome"], registry=REGISTRY)
ABSTENTIONS = Counter("agent_abstentions_total", "Abstentions by reason",
                      ["architecture", "reason"], registry=REGISTRY)
GATE_FAILURES = Counter("gate_check_failures_total", "Verification-gate failures by check",
                        ["architecture", "check"], registry=REGISTRY)
TOOL_CALLS = Counter("tool_calls_total", "Tool calls by tool and result", ["tool", "ok"], registry=REGISTRY)
RETRIES = Counter("multiagent_retries_total", "Role retries in the multi-agent pipeline",
                  ["role"], registry=REGISTRY)
LATENCY = Histogram("agent_run_seconds", "End-to-end agent latency", ["architecture"],
                    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
                    registry=REGISTRY)

REASONS = {"verification_failed", "write_request_refused", "no_evidence", "planner_error", "tool_error",
           "step_budget_exhausted", "render_unsupported", "unstructured_output", "malformed_output",
           "out_of_scope", "invalid_handoff"}


def record(result: Any) -> None:
    """Count one finished run (an ``AgentResult``)."""
    arch = result.architecture
    if not result.abstained:
        outcome = "answered"
    elif result.reason == "verification_failed":
        outcome = "rejected"
    else:
        outcome = "abstained"
    RUNS.labels(arch, outcome).inc()
    if result.abstained:
        reason = result.reason if result.reason in REASONS else (
            "unresolved" if str(result.reason).startswith("unresolved") else "other")
        ABSTENTIONS.labels(arch, reason).inc()
    for step in result.steps:
        if step.get("type") == "verification_rejected":
            for check in step.get("checks_failed", []):
                if check in CHECKS:
                    GATE_FAILURES.labels(arch, check).inc()
        elif step.get("type") == "tool_call":
            tool = step.get("tool") if step.get("tool") in TOOLS else "unknown"
            TOOL_CALLS.labels(tool, "true" if step.get("ok") else "false").inc()
        elif step.get("type") == "retry":
            RETRIES.labels(step.get("role", "unknown")).inc()
    LATENCY.labels(arch).observe(result.latency_ms / 1000)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
