"""Knowledge graph of companies, filings and versioned claims.

The relational claim store is the system of record; this module projects it into a
property graph so lineage and restatement questions become path queries:

    (:Company)-[:FILED]->(:Filing)-[:REPORTS]->(:Claim)-[:ABOUT]->(:Company)
    (:Filing)-[:AMENDS]->(:Filing)                 a 10-K/A amends its 10-K
    (:Claim)-[:SUPERSEDES {change}]->(:Claim)      a later version replaces an earlier one

Two stores answer the same queries: ``MemoryGraph`` (built in-process, used by the API
and the tests) and ``Neo4jGraph`` (``python -m app.graph sync`` pushes the projection to
Neo4j with idempotent MERGEs). ``tests/test_graph.py`` checks that the graph agrees with
the relational store on every as-of boundary, and that Neo4j agrees with the in-memory
graph on every query.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import claims as q
from .database import companies, filings

# Claim properties copied into the graph. Filing text (untrusted_source_text) is left out:
# the graph is for structure and lineage, not for content a model will read.
CLAIM_PROPS = ("claim_id", "ticker", "metric", "fiscal_period", "value", "unit", "display",
               "version", "status", "is_current")


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)   # id -> {"label", **props}
    edges: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)  # (src, type, dst, props)

    def add_node(self, node_id: str, label: str, **props: Any) -> None:
        self.nodes[node_id] = {"label": label, **props}

    def add_edge(self, src: str, rel: str, dst: str, **props: Any) -> None:
        self.edges.append((src, rel, dst, props))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in self.nodes.values():
            out[n["label"]] = out.get(n["label"], 0) + 1
        for _, rel, _, _ in self.edges:
            out[rel] = out.get(rel, 0) + 1
        return out


def build_graph(conn: Connection) -> Graph:
    """Project the relational claim store into a Graph."""
    g = Graph()
    for row in conn.execute(select(companies)).all():
        g.add_node(f"company:{row.ticker}", "Company", ticker=row.ticker, name=row.name)
    for row in conn.execute(select(filings).order_by(filings.c.filed_at, filings.c.id)).all():
        fid = f"filing:{row.accession}"
        g.add_node(fid, "Filing", accession=row.accession, ticker=row.ticker, form_type=row.form_type,
                   filed_at=row.filed_at.isoformat(), source_uri=row.source_uri, sha256=row.sha256)
        g.add_edge(f"company:{row.ticker}", "FILED", fid)
        if row.amends_accession:
            g.add_edge(fid, "AMENDS", f"filing:{row.amends_accession}")

    for company in q.list_companies(conn):
        for key in q.available_periods(conn, company["ticker"]):
            history = q.claim_history(conn, company["ticker"], key["metric"], key["fiscal_period"])
            previous = None
            for item in history:
                cid = f"claim:{item['claim_id']}"
                g.add_node(cid, "Claim", **{k: item[k] for k in CLAIM_PROPS})
                g.add_edge(f"filing:{item['accession']}", "REPORTS", cid)
                g.add_edge(cid, "ABOUT", f"company:{item['ticker']}")
                if previous is not None:
                    g.add_edge(cid, "SUPERSEDES", f"claim:{previous['claim_id']}",
                               change=item.get("change", "0"), status=item["status"])
                previous = item
    return g


def _claim_view(node: dict[str, Any], filing: dict[str, Any]) -> dict[str, Any]:
    out = {k: node[k] for k in CLAIM_PROPS}
    out.update(accession=filing["accession"], form_type=filing["form_type"], filed_at=filing["filed_at"])
    return out


class MemoryGraph:
    """Graph queries over an in-process Graph. Same results as Neo4jGraph."""

    name = "memory"

    def __init__(self, graph: Graph):
        self.g = graph
        self.out: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
        self.inc: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
        for src, rel, dst, props in graph.edges:
            self.out.setdefault((src, rel), []).append((dst, props))
            self.inc.setdefault((dst, rel), []).append((src, props))

    def _filing_of(self, claim_id: str) -> dict[str, Any]:
        (fid, _), = self.inc[(claim_id, "REPORTS")]
        return self.g.nodes[fid]

    def _claims(self, ticker: str, metric: str, fiscal_period: str) -> list[str]:
        ids = [nid for nid, n in self.g.nodes.items() if n["label"] == "Claim" and n["ticker"] == ticker.upper()
               and n["metric"] == metric and n["fiscal_period"] == fiscal_period]
        return sorted(ids, key=lambda nid: self.g.nodes[nid]["version"])

    def restatement_chain(self, ticker: str, metric: str, fiscal_period: str) -> list[dict[str, Any]]:
        """Every version of one figure, oldest first, with the filing that published it."""
        return [_claim_view(self.g.nodes[c], self._filing_of(c)) for c in self._claims(ticker, metric, fiscal_period)]

    def as_of(self, ticker: str, metric: str, fiscal_period: str, as_of: date) -> dict[str, Any] | None:
        """The latest version whose filing was published on or before ``as_of``."""
        visible = [c for c in self.restatement_chain(ticker, metric, fiscal_period)
                   if date.fromisoformat(c["filed_at"]) <= as_of]
        return visible[-1] if visible else None

    def lineage(self, claim_id: int) -> dict[str, Any] | None:
        """Claim -> filing -> chain of amended filings -> company."""
        cid = f"claim:{claim_id}"
        if cid not in self.g.nodes:
            return None
        filing = self._filing_of(cid)
        chain, fid = [], f"filing:{filing['accession']}"
        while (fid, "AMENDS") in self.out:
            fid = self.out[(fid, "AMENDS")][0][0]
            chain.append(self.g.nodes[fid]["accession"])
        superseded = [self.g.nodes[d]["claim_id"] for d, _ in self.out.get((cid, "SUPERSEDES"), [])]
        superseded_by = [self.g.nodes[s]["claim_id"] for s, _ in self.inc.get((cid, "SUPERSEDES"), [])]
        company = self.g.nodes[f"company:{self.g.nodes[cid]['ticker']}"]
        return {"claim": _claim_view(self.g.nodes[cid], filing),
                "source_uri": filing["source_uri"], "sha256": filing["sha256"],
                "amends_chain": chain, "supersedes": superseded, "superseded_by": superseded_by,
                "company": {"ticker": company["ticker"], "name": company["name"]}}

    def amendment_impact(self, accession: str) -> list[dict[str, Any]]:
        """What one filing changed: each claim it reports and the version it replaced."""
        fid = f"filing:{accession}"
        out = []
        for cid, _ in sorted(self.out.get((fid, "REPORTS"), []), key=lambda t: self.g.nodes[t[0]]["claim_id"]):
            node = self.g.nodes[cid]
            for prev, props in self.out.get((cid, "SUPERSEDES"), []):
                out.append({"metric": node["metric"], "fiscal_period": node["fiscal_period"],
                            "status": props["status"], "from_value": self.g.nodes[prev]["value"],
                            "to_value": node["value"], "change": props["change"],
                            "replaces_accession": self._filing_of(prev)["accession"]})
        return out

    def restated_companies(self) -> list[dict[str, Any]]:
        """Companies with at least one restated figure, with the count and the metrics affected."""
        found: dict[str, dict[str, Any]] = {}
        for n in self.g.nodes.values():
            if n["label"] == "Claim" and n["status"] == "restated":
                entry = found.setdefault(n["ticker"], {"ticker": n["ticker"], "restatements": 0, "metrics": set()})
                entry["restatements"] += 1
                entry["metrics"].add(n["metric"])
        return [{**e, "metrics": sorted(e["metrics"])} for _, e in sorted(found.items())]


class Neo4jGraph:
    """The same projection and queries in Neo4j (``pip install neo4j``)."""

    name = "neo4j"

    CONSTRAINTS = (
        "CREATE CONSTRAINT company_ticker IF NOT EXISTS FOR (n:Company) REQUIRE n.ticker IS UNIQUE",
        "CREATE CONSTRAINT filing_accession IF NOT EXISTS FOR (n:Filing) REQUIRE n.accession IS UNIQUE",
        "CREATE CONSTRAINT claim_id IF NOT EXISTS FOR (n:Claim) REQUIRE n.claim_id IS UNIQUE",
        "CREATE INDEX claim_key IF NOT EXISTS FOR (n:Claim) ON (n.ticker, n.metric, n.fiscal_period)",
    )
    KEYS = {"Company": "ticker", "Filing": "accession", "Claim": "claim_id"}

    def __init__(self, uri: str, user: str, password: str, database: str | None = None):
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    @classmethod
    def from_env(cls) -> Neo4jGraph:
        return cls(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                   os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"],
                   os.environ.get("NEO4J_DATABASE"))

    def close(self) -> None:
        self.driver.close()

    def _run(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = self.driver.execute_query(cypher, params, database_=self.database)
        return [r.data() for r in records]

    def sync(self, graph: Graph) -> dict[str, int]:
        """Idempotent upsert of the projection (MERGE on each label's key)."""
        for stmt in self.CONSTRAINTS:
            self._run(stmt)
        by_label: dict[str, list[dict[str, Any]]] = {}
        for node in graph.nodes.values():
            props = {k: v for k, v in node.items() if k != "label"}
            by_label.setdefault(node["label"], []).append(props)
        for label, rows in by_label.items():
            key = self.KEYS[label]
            date_fix = ", n.filed_at = date(row.filed_at)" if label == "Filing" else ""
            self._run(f"UNWIND $rows AS row MERGE (n:{label} {{{key}: row.{key}}}) SET n += row{date_fix}",
                      rows=rows)
        ends = {"FILED": ("Company", "Filing"), "AMENDS": ("Filing", "Filing"), "REPORTS": ("Filing", "Claim"),
                "ABOUT": ("Claim", "Company"), "SUPERSEDES": ("Claim", "Claim")}
        by_rel: dict[str, list[dict[str, Any]]] = {}
        for src, rel, dst, props in graph.edges:
            s, d = graph.nodes.get(src), graph.nodes.get(dst)
            if s is None or d is None:
                continue
            by_rel.setdefault(rel, []).append({"src": s[self.KEYS[s["label"]]], "dst": d[self.KEYS[d["label"]]],
                                               "props": props})
        for rel, rows in by_rel.items():
            sl, dl = ends[rel]
            self._run(f"UNWIND $rows AS row MATCH (a:{sl} {{{self.KEYS[sl]}: row.src}}) "
                      f"MATCH (b:{dl} {{{self.KEYS[dl]}: row.dst}}) MERGE (a)-[r:{rel}]->(b) SET r += row.props",
                      rows=rows)
        return graph.counts()

    def counts(self) -> dict[str, int]:
        out = {r["label"]: r["n"] for r in self._run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n")}
        out.update({r["rel"]: r["n"] for r in self._run("MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n")})
        return out

    _VIEW = ("c {.claim_id, .ticker, .metric, .fiscal_period, .value, .unit, .display, .version, .status, "
             ".is_current, accession: f.accession, form_type: f.form_type, filed_at: toString(f.filed_at)} AS c")

    def restatement_chain(self, ticker: str, metric: str, fiscal_period: str) -> list[dict[str, Any]]:
        rows = self._run("MATCH (f:Filing)-[:REPORTS]->(c:Claim {ticker: $t, metric: $m, fiscal_period: $p}) "
                         f"RETURN {self._VIEW} ORDER BY c.version", t=ticker.upper(), m=metric, p=fiscal_period)
        return [r["c"] for r in rows]

    def as_of(self, ticker: str, metric: str, fiscal_period: str, as_of: date) -> dict[str, Any] | None:
        rows = self._run("MATCH (f:Filing)-[:REPORTS]->(c:Claim {ticker: $t, metric: $m, fiscal_period: $p}) "
                         f"WHERE f.filed_at <= date($d) RETURN {self._VIEW} ORDER BY c.version DESC LIMIT 1",
                         t=ticker.upper(), m=metric, p=fiscal_period, d=as_of.isoformat())
        return rows[0]["c"] if rows else None

    def lineage(self, claim_id: int) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (f:Filing)-[:REPORTS]->(c:Claim {claim_id: $id})-[:ABOUT]->(co:Company) "
            "OPTIONAL MATCH p = (f)-[:AMENDS*1..]->(:Filing) "
            "WITH f, c, co, p ORDER BY length(p) DESC "
            "WITH f, c, co, collect(p)[0] AS longest "
            "OPTIONAL MATCH (c)-[:SUPERSEDES]->(older:Claim) "
            "OPTIONAL MATCH (newer:Claim)-[:SUPERSEDES]->(c) "
            f"RETURN {self._VIEW}, f.source_uri AS source_uri, f.sha256 AS sha256, "
            "CASE WHEN longest IS NULL THEN [] ELSE [n IN tail(nodes(longest)) | n.accession] END AS chain, "
            "collect(DISTINCT older.claim_id) AS supersedes, collect(DISTINCT newer.claim_id) AS superseded_by, "
            "co {.ticker, .name} AS company", id=claim_id)
        if not rows:
            return None
        r = rows[0]
        return {"claim": r["c"], "source_uri": r["source_uri"], "sha256": r["sha256"], "amends_chain": r["chain"],
                "supersedes": r["supersedes"], "superseded_by": r["superseded_by"], "company": r["company"]}

    def amendment_impact(self, accession: str) -> list[dict[str, Any]]:
        rows = self._run(
            "MATCH (:Filing {accession: $a})-[:REPORTS]->(c:Claim)-[s:SUPERSEDES]->(prev:Claim)<-[:REPORTS]-(pf:Filing) "
            "RETURN c.metric AS metric, c.fiscal_period AS fiscal_period, s.status AS status, "
            "prev.value AS from_value, c.value AS to_value, s.change AS change, pf.accession AS replaces_accession "
            "ORDER BY c.claim_id", a=accession)
        return rows

    def restated_companies(self) -> list[dict[str, Any]]:
        rows = self._run("MATCH (c:Claim {status: 'restated'}) "
                         "WITH c.ticker AS ticker, count(c) AS restatements, collect(DISTINCT c.metric) AS metrics "
                         "RETURN ticker, restatements, metrics ORDER BY ticker")
        return [{**r, "metrics": sorted(r["metrics"])} for r in rows]


def main(argv: list[str] | None = None) -> int:
    from .database import make_engine, readonly_connection

    parser = argparse.ArgumentParser(description="Project the claim store into a knowledge graph.")
    parser.add_argument("command", choices=["summary", "sync"],
                        help="summary: print node/edge counts; sync: upsert into Neo4j (NEO4J_URI, NEO4J_PASSWORD)")
    args = parser.parse_args(argv)
    with readonly_connection(make_engine()) as conn:
        graph = build_graph(conn)
    if args.command == "summary":
        print(json.dumps(graph.counts(), indent=2, sort_keys=True))
        return 0
    store = Neo4jGraph.from_env()
    try:
        store.sync(graph)
        print(json.dumps(store.counts(), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
