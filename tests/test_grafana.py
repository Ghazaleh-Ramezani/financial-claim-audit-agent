"""The Grafana dashboard queries only metrics and labels the service defines, through the provisioned datasource."""

import json
import re
from pathlib import Path

import yaml

from app import metrics, stream

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "grafana"
DASHBOARD = json.loads((ROOT / "dashboards" / "claim-audit.json").read_text())
SUFFIX = re.compile(r"_(total|bucket|sum|count)$")
METRIC = re.compile(r"\b((?:agent|gate|tool|multiagent|stream)_\w+)\b")
DEFINED = {m._name: set(m._labelnames) for m in (
    metrics.RUNS, metrics.ABSTENTIONS, metrics.GATE_FAILURES, metrics.TOOL_CALLS, metrics.RETRIES,
    metrics.LATENCY, stream.MESSAGES, stream.RESTATEMENTS)}


def queries():
    for panel in DASHBOARD["panels"]:
        for target in panel.get("targets", []):
            yield panel["title"], target["expr"]


def test_every_query_uses_defined_metrics_and_labels():
    seen = set()
    for title, expr in queries():
        used = set(METRIC.findall(expr))
        assert used, title
        allowed = {"le"}
        for metric in used:
            base = SUFFIX.sub("", metric)
            assert base in DEFINED, (title, metric)
            allowed |= DEFINED[base]
            seen.add(base)
        for name in re.findall(r"(\w+)\s*(?:!=|=~|=)\s*\"", expr):
            assert name in allowed, (title, name)
        for group in re.findall(r"by \(([^)]*)\)", expr):
            for name in (x.strip() for x in group.split(",")):
                assert name in allowed, (title, name)
        for name in re.findall(r"\{\{(\w+)\}\}", next(
                (t.get("legendFormat", "") for p in DASHBOARD["panels"] if p["title"] == title
                 for t in p.get("targets", []) if t["expr"] == expr), "")):
            assert name in allowed, (title, name)
    assert seen == set(DEFINED), set(DEFINED) - seen     # every service metric is on the dashboard


def test_panels_use_the_provisioned_datasource_and_unique_ids():
    ds = yaml.safe_load((ROOT / "provisioning" / "datasources" / "prometheus.yml").read_text())["datasources"][0]
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))
    for panel in DASHBOARD["panels"]:
        if panel["type"] != "row":
            assert panel["datasource"]["uid"] == ds["uid"], panel["title"]


def test_dashboard_provider_points_at_the_mounted_folder():
    provider = yaml.safe_load((ROOT / "provisioning" / "dashboards" / "dashboards.yml").read_text())["providers"][0]
    compose = yaml.safe_load((ROOT.parents[1] / "docker-compose.yml").read_text())
    mounts = compose["services"]["grafana"]["volumes"]
    assert f"./deploy/grafana/dashboards:{provider['options']['path']}:ro" in mounts
