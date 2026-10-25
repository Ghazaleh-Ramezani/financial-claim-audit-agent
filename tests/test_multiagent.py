"""Multi-agent pipeline: contracts, the trust boundary, failure routing, API and metrics.

LLM roles are exercised against scripted OpenAI-compatible endpoints
(httpx.MockTransport); the deterministic roles run as-is.
"""

import json
from datetime import date

import httpx
from fastapi.testclient import TestClient

from app.agent import run_agent
from app.main import create_app
from app.multiagent import LLMAnalyst, LLMRetriever, LLMWriter, MultiAgentSystem
from app.planners import RulePlanner

ACME_REV = {"ticker": "ACME", "metric": "revenue", "fiscal_period": "FY2025"}
CURRENT_REV = "What is ACME's current FY2025 revenue?"


def endpoint(turns):
    """Scripted /chat/completions; records every request body."""
    seen = {"requests": []}

    def handler(request: httpx.Request):
        seen["requests"].append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": turns[len(seen["requests"]) - 1]}]})

    return httpx.MockTransport(handler), seen


def tool(name, args, call_id="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def reply(obj):
    return {"role": "assistant", "content": json.dumps(obj)}


def client(transport):
    return {"model": "scripted", "base_url": "http://scripted/v1", "api_key": "x", "transport": transport}


def steps_of(r, kind):
    return [s for s in r.steps if s.get("type") == kind]


def claim_ids(engine):
    from app import claims as q
    from app.database import readonly_connection
    with readonly_connection(engine) as conn:
        return (q.original_claim(conn, "ACME", "revenue", "FY2025")["claim_id"],
                q.claim_as_of(conn, "ACME", "revenue", "FY2025", None)["claim_id"])


def test_rule_pipeline_matches_the_single_agent_on_every_case(loaded):
    system = MultiAgentSystem()
    cases = [json.loads(line) for line in open("evaluations/cases.jsonl")]
    for c in cases:
        as_of = date.fromisoformat(c["as_of"]) if c.get("as_of") else None
        single = run_agent(loaded, RulePlanner(), c["question"], as_of)
        multi = system.run(loaded, c["question"], as_of)
        assert multi.abstained == single.abstained, c["id"]
        if not single.abstained:
            assert multi.answer == single.answer and multi.citations == single.citations, c["id"]


def test_document_text_reaches_only_the_retriever(loaded):
    # The Initech filing carries an injected instruction in its source text.
    writer_transport, seen = endpoint([reply({"answer": "Initech FY2025 revenue was $64.8 million.",
                                              "citations": [6]})])
    system = MultiAgentSystem(writer=LLMWriter(**client(writer_transport)))
    r = system.run(loaded, "What was Initech revenue for FY2025?")
    exposure = steps_of(r, "exposure")[0]["untrusted_chars"]
    assert exposure["retriever"] > 0 and exposure["analyst"] == exposure["writer"] == 0
    writer_input = json.dumps(seen["requests"])
    assert "untrusted_source_text" not in writer_input and "disregard the database" not in writer_input
    assert not r.abstained and "$64.8 million" in r.answer


def test_evidence_failures_go_back_to_the_retriever(loaded):
    original, restated = claim_ids(loaded)
    transport, seen = endpoint([
        tool("get_claim_history", ACME_REV),
        reply({"claim_ids": [original], "intent": "current"}),   # stale: superseded version
        reply({"claim_ids": [restated], "intent": "current"}),   # after the verifier's feedback
    ])
    r = MultiAgentSystem(retriever=LLMRetriever(**client(transport))).run(loaded, CURRENT_REV)
    assert not r.abstained and "$115 million" in r.answer
    retry = steps_of(r, "retry")[0]
    assert retry["role"] == "retriever" and "superseded" in retry["why"]
    assert "rejected that selection" in json.dumps(seen["requests"][-1]["messages"][-1])


def test_wording_failures_go_back_to_the_writer(loaded):
    _, restated = claim_ids(loaded)
    transport, _ = endpoint([
        reply({"answer": "The current ACME FY2025 revenue is $120 million.", "citations": [restated]}),
        reply({"answer": "The current ACME FY2025 revenue is $115 million.", "citations": [restated]}),
    ])
    r = MultiAgentSystem(writer=LLMWriter(**client(transport))).run(loaded, CURRENT_REV)
    assert not r.abstained and "$115 million" in r.answer
    assert [s["role"] for s in steps_of(r, "retry")] == ["writer"]


def test_a_retriever_cannot_pass_text_through_its_contract(loaded):
    # Extra fields and a free-text reason both violate the schema; after one retry the system abstains
    # with its own fixed text, so nothing the retriever wrote reaches the user.
    transport, _ = endpoint([
        reply({"claim_ids": [8], "intent": "current", "note": "Visit acme-verify.example"}),
        reply({"abstain": True, "reason": "Visit acme-verify.example to see the data"}),
    ])
    r = MultiAgentSystem(retriever=LLMRetriever(**client(transport))).run(loaded, CURRENT_REV)
    assert r.abstained and r.reason == "invalid_handoff" and "acme-verify" not in r.answer


def test_unretrieved_claims_and_citations_outside_the_facts_are_rejected(loaded):
    original, restated = claim_ids(loaded)
    transport, _ = endpoint([reply({"claim_ids": [9999], "intent": "current"}),
                             reply({"claim_ids": [9999], "intent": "current"})])
    r = MultiAgentSystem(retriever=LLMRetriever(**client(transport))).run(loaded, CURRENT_REV)
    assert r.abstained and "not returned by any tool" in steps_of(r, "retry")[0]["why"]

    transport, _ = endpoint([reply({"answer": "ACME FY2025 revenue is $120 million.", "citations": [original]})] * 2)
    r = MultiAgentSystem(writer=LLMWriter(**client(transport))).run(loaded, CURRENT_REV)
    assert r.abstained and "not among the facts" in steps_of(r, "retry")[0]["why"]


def test_llm_analyst_chooses_from_the_menu_and_code_computes(loaded):
    transport, seen = endpoint([reply({"plan": "restatement_change"})])
    r = MultiAgentSystem(analyst=LLMAnalyst(**client(transport))).run(loaded, CURRENT_REV)
    handoff = next(s for s in r.steps if s.get("role") == "analyst" and s["type"] == "handoff")
    assert handoff["result"]["restatements"][0]["change"] == "-5000000"
    assert "untrusted_source_text" not in json.dumps(seen["requests"])
    # An answer outside the menu is a failed hand-off; the analysis degrades and the answer is unaffected.
    transport, _ = endpoint([reply({"plan": "compute_whatever_the_filing_says"})])
    r = MultiAgentSystem(analyst=LLMAnalyst(**client(transport))).run(loaded, CURRENT_REV)
    assert any(s.get("role") == "analyst" and s["type"] == "handoff_failed" for s in r.steps)
    assert not r.abstained and "$115 million" in r.answer


def test_retries_are_bounded(loaded):
    original, _ = claim_ids(loaded)
    stale = reply({"claim_ids": [original], "intent": "current"})
    transport, seen = endpoint([tool("get_claim_history", ACME_REV), stale, stale, stale])
    r = MultiAgentSystem(retriever=LLMRetriever(**client(transport)), max_retries=1).run(loaded, CURRENT_REV)
    assert r.abstained and r.reason == "verification_failed"
    assert len(steps_of(r, "retry")) == 1 and len(seen["requests"]) == 3


def test_api_multi_architecture_metrics_and_readiness(loaded):
    app = create_app(engine=loaded, planner=RulePlanner())
    with TestClient(app) as c:
        body = c.post("/ask", json={"question": CURRENT_REV, "architecture": "multi"}).json()
        assert body["architecture"] == "multi" and "$115 million" in body["answer"]
        assert c.post("/ask", json={"question": "Delete the restated ACME revenue."}).json()["abstained"]
        assert c.get("/ready").json() == {"ready": True, "claims": 10}
        r = c.get("/health", headers={"X-Request-ID": "req-123"})
        assert r.headers["X-Request-ID"] == "req-123"
        text = c.get("/metrics").text
    assert 'agent_runs_total{architecture="multi",outcome="answered"}' in text
    assert 'agent_abstentions_total{architecture="single",reason="write_request_refused"}' in text
    assert "agent_run_seconds_bucket" in text


def test_ready_reports_an_empty_store(engine):
    with TestClient(create_app(engine=engine, planner=RulePlanner())) as c:
        r = c.get("/ready")
    assert r.status_code == 503 and not r.json()["ready"]
