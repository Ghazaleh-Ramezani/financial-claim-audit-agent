"""Knowledge graph: structure, agreement with the relational store, and Neo4j parity."""

import os
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import claims as q
from app.database import readonly_connection
from app.graph import MemoryGraph, build_graph
from app.main import create_app
from app.planners import RulePlanner


@pytest.fixture
def graph(loaded):
    with readonly_connection(loaded) as conn:
        return build_graph(conn)


@pytest.fixture
def kg(graph):
    return MemoryGraph(graph)


def all_keys(conn):
    return [(c["ticker"], k["metric"], k["fiscal_period"])
            for c in q.list_companies(conn) for k in q.available_periods(conn, c["ticker"])]


def boundaries(conn):
    """Every filing date, the day before it, and a date after all filings."""
    days = {r[0] for r in conn.exec_driver_sql("SELECT filed_at FROM filings")}
    days = {d if isinstance(d, date) else date.fromisoformat(d) for d in days}
    return sorted(days | {d - timedelta(days=1) for d in days} | {max(days) + timedelta(days=30)})


def test_projection_counts(graph):
    counts = graph.counts()
    assert counts["Company"] == 3 and counts["Filing"] == 5 and counts["Claim"] == 10
    assert counts["FILED"] == 5 and counts["REPORTS"] == 10 and counts["ABOUT"] == 10
    assert counts["AMENDS"] == 1
    assert counts["SUPERSEDES"] == 3   # revenue and net income restated, operating income reaffirmed


def test_graph_agrees_with_relational_store_on_every_as_of_boundary(loaded, kg):
    with readonly_connection(loaded) as conn:
        checked = 0
        for key in all_keys(conn):
            for d in boundaries(conn):
                expected = q.claim_as_of(conn, *key, d)
                got = kg.as_of(*key, d)
                assert (got and got["claim_id"]) == (expected and expected["claim_id"]), (key, d)
                checked += 1
    assert checked > 50


def test_restatement_chain(kg):
    chain = kg.restatement_chain("ACME", "revenue", "FY2025")
    assert [(c["status"], Decimal(c["value"])) for c in chain] == [("original", Decimal(120_000_000)),
                                                                    ("restated", Decimal(115_000_000))]
    assert [c["accession"] for c in chain] == ["SYN-ACME-26-000101", "SYN-ACME-26-000187"]
    assert chain[-1]["is_current"] and not chain[0]["is_current"]


def test_lineage_walks_amendment_and_supersession(kg):
    current = kg.restatement_chain("ACME", "revenue", "FY2025")[-1]
    original = kg.restatement_chain("ACME", "revenue", "FY2025")[0]
    lin = kg.lineage(current["claim_id"])
    assert lin["claim"]["accession"] == "SYN-ACME-26-000187"
    assert lin["amends_chain"] == ["SYN-ACME-26-000101"]
    assert lin["supersedes"] == [original["claim_id"]] and lin["superseded_by"] == []
    assert lin["company"] == {"ticker": "ACME", "name": "Acme Analytics Inc."}
    assert len(lin["sha256"]) == 64
    assert kg.lineage(original["claim_id"])["superseded_by"] == [current["claim_id"]]
    assert kg.lineage(10_000) is None


def test_amendment_impact(kg):
    impact = {i["metric"]: i for i in kg.amendment_impact("SYN-ACME-26-000187")}
    assert set(impact) == {"revenue", "net_income", "operating_income"}
    assert impact["revenue"]["status"] == "restated" and Decimal(impact["revenue"]["change"]) == -5_000_000
    assert impact["operating_income"]["status"] == "reaffirmed" and Decimal(impact["operating_income"]["change"]) == 0
    assert all(i["replaces_accession"] == "SYN-ACME-26-000101" for i in impact.values())
    assert kg.amendment_impact("SYN-ACME-26-000101") == []   # an original replaces nothing


def test_restated_companies(kg):
    assert kg.restated_companies() == [{"ticker": "ACME", "restatements": 2, "metrics": ["net_income", "revenue"]}]


def test_graph_carries_no_filing_text(graph):
    for node in graph.nodes.values():
        assert "untrusted_source_text" not in node and "narrative" not in node


def test_graph_endpoints(loaded):
    with TestClient(create_app(engine=loaded, planner=RulePlanner())) as c:
        assert c.get("/graph/summary").json()["SUPERSEDES"] == 3
        assert c.get("/graph/restated-companies").json()[0]["ticker"] == "ACME"
        impact = c.get("/graph/impact/SYN-ACME-26-000187").json()
        assert {i["metric"] for i in impact} == {"revenue", "net_income", "operating_income"}
        cid = c.get("/claims/ACME/revenue/FY2025").json()["claim_id"]
        assert c.get(f"/graph/lineage/{cid}").json()["amends_chain"] == ["SYN-ACME-26-000101"]
        assert c.get("/graph/lineage/999999").status_code == 404


@pytest.mark.skipif(not os.environ.get("NEO4J_URI"), reason="set NEO4J_URI/NEO4J_PASSWORD to run against Neo4j")
def test_neo4j_matches_memory_graph(loaded, graph, kg):
    from app.graph import Neo4jGraph

    store = Neo4jGraph.from_env()
    try:
        store._run("MATCH (n) DETACH DELETE n")
        store.sync(graph)
        first = store.counts()
        store.sync(graph)                               # idempotent: a second sync changes nothing
        assert store.counts() == first == graph.counts()
        with readonly_connection(loaded) as conn:
            for key in all_keys(conn):
                assert store.restatement_chain(*key) == kg.restatement_chain(*key), key
                for d in boundaries(conn):
                    assert store.as_of(*key, d) == kg.as_of(*key, d), (key, d)
        for node in graph.nodes.values():
            if node["label"] == "Claim":
                assert store.lineage(node["claim_id"]) == kg.lineage(node["claim_id"]), node["claim_id"]
            if node["label"] == "Filing":
                assert store.amendment_impact(node["accession"]) == kg.amendment_impact(node["accession"])
        assert store.restated_companies() == kg.restated_companies()
    finally:
        store.close()
