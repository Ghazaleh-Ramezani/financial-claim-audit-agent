from fastapi.testclient import TestClient

from app.main import create_app
from app.planners import RulePlanner


def client(engine):
    return TestClient(create_app(engine=engine, planner=RulePlanner()))


def test_health(loaded):
    with client(loaded) as c:
        body = c.get("/health").json()
    assert body["status"] == "ok" and body["filings"] == 5 and body["claims"] == 10


def test_ask_returns_answer_citations_and_trace(loaded):
    with client(loaded) as c:
        body = c.post("/ask", json={"question": "What revenue did ACME originally report for FY2025?"}).json()
    assert not body["abstained"] and body["trace_id"]
    assert body["citations"][0]["accession"] == "SYN-ACME-26-000101"
    assert [s["tool"] for s in body["steps"]] == ["list_companies", "get_claim_history"]


def test_ask_as_of_field(loaded):
    with client(loaded) as c:
        body = c.post("/ask", json={"question": "ACME FY2025 revenue?", "as_of": "2026-03-01",
                                    "include_trace": False}).json()
    assert "$120 million" in body["answer"] and "steps" not in body


def test_claim_endpoints(loaded):
    with client(loaded) as c:
        assert c.get("/claims/ACME/revenue/FY2025").json()["value"] == "115000000"
        assert c.get("/claims/ACME/revenue/FY2025", params={"as_of": "2026-03-01"}).json()["value"] == "120000000"
        assert c.get("/claims/ACME/revenue/FY2025", params={"as_of": "2026-01-01"}).status_code == 404
        assert len(c.get("/claims/ACME/revenue/FY2025/history").json()) == 2
        assert c.get("/claims/ACME/bogus/FY2025").status_code == 422
        assert len(c.get("/restatements").json()) == 2


def test_api_has_no_write_routes(loaded):
    app = create_app(engine=loaded, planner=RulePlanner())
    methods = {m for r in app.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD", "POST"}
    posts = [r.path for r in app.routes if "POST" in getattr(r, "methods", set())]
    assert posts == ["/ask"]
