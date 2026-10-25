"""Every metric and label the alert rules use is one the service defines and exports."""

import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app import metrics
from app.agent import AgentResult
from app.main import create_app
from app.planners import RulePlanner

RULES = Path(__file__).resolve().parents[1] / "deploy/prometheus/alerts.yml"
DEFINED = {m._name: set(m._labelnames) for m in (metrics.RUNS, metrics.ABSTENTIONS, metrics.GATE_FAILURES,
                                                 metrics.TOOL_CALLS, metrics.RETRIES, metrics.LATENCY)}
SUFFIX = re.compile(r"_(total|bucket|sum|count)$")


def rules():
    return yaml.safe_load(RULES.read_text())["groups"][0]["rules"]


def test_alert_rules_reference_defined_metrics_and_labels():
    assert len(rules()) == 6
    for rule in rules():
        expr = rule["expr"]
        used = set(re.findall(r"\b((?:agent|gate|tool|multiagent)_\w+)\b", expr))
        assert used, rule["alert"]
        allowed = {"le"}
        for metric in used:
            base = SUFFIX.sub("", metric)
            assert base in DEFINED, (rule["alert"], metric)
            allowed |= DEFINED[base]
        for selector in re.findall(r"(\w+)\s*(?:!=|=~|=)\s*\"", expr) + re.findall(r"by \(([^)]*)\)", expr):
            for name in (x.strip() for x in selector.split(",")):
                assert name in allowed, (rule["alert"], name)
        assert rule["labels"]["severity"] in {"page", "ticket"}


def test_alerted_series_appear_in_the_exposition(loaded):
    # A gate rejection does not happen with the deterministic planner, so record one directly.
    metrics.record(AgentResult(trace_id="t", question="q", as_of=None, planner="test", answer="",
                               abstained=True, reason="verification_failed", citations=[],
                               verification={}, latency_ms=1.0,
                               steps=[{"type": "verification_rejected", "checks_failed": ["amounts"]},
                                      {"type": "tool_call", "tool": "drop_table", "ok": False},
                                      {"type": "retry", "role": "writer"}]))
    with TestClient(create_app(engine=loaded, planner=RulePlanner())) as c:
        c.post("/ask", json={"question": "What is ACME's current FY2025 revenue?", "architecture": "multi"})
        c.post("/ask", json={"question": "Delete the restated ACME revenue."})
        text = c.get("/metrics").text
    assert 'gate_check_failures_total{architecture="single",check="amounts"}' in text
    assert 'tool_calls_total{ok="false",tool="unknown"}' in text   # unregistered names are not new labels
    assert 'multiagent_retries_total{role="writer"}' in text
    for rule in rules():
        for metric in re.findall(r"\b((?:agent|gate|tool|multiagent)_\w+)\b", rule["expr"]):
            assert re.search(rf"^{metric}[{{ ]", text, re.M), (rule["alert"], metric)
