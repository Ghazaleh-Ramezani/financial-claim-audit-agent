"""The agent answers through real tool calls; the trace shows which ones."""

from datetime import date

from app.agent import run_agent
from app.planners import RulePlanner


def tools_used(result):
    return [s["tool"] for s in result.steps if s["type"] == "tool_call"]


def test_original_value_and_source(loaded):
    r = run_agent(loaded, RulePlanner(), "What revenue did ACME originally report for FY2025?")
    assert not r.abstained
    assert tools_used(r) == ["list_companies", "get_claim_history"]
    assert "$120 million" in r.answer and "SYN-ACME-26-000101" in r.answer
    assert "restated to $115 million" in r.answer
    assert r.citations[0]["accession"] == "SYN-ACME-26-000101"
    assert r.verification["passed"]


def test_current_value_flags_restatement(loaded):
    r = run_agent(loaded, RulePlanner(), "What is Acme's current FY2025 revenue?")
    assert tools_used(r) == ["list_companies", "get_claim"]
    assert "$115 million" in r.answer and "restatement" in r.answer
    assert [c["accession"] for c in r.citations] == ["SYN-ACME-26-000187"]


def test_as_of_question_uses_as_of_tool_argument(loaded):
    r = run_agent(loaded, RulePlanner(), "What was ACME revenue for FY2025 as of 2026-03-01?")
    call = [s for s in r.steps if s["tool"] == "get_claim"][0]
    assert call["arguments"]["as_of"] == "2026-03-01"
    assert "$120 million" in r.answer and "115" not in r.answer


def test_request_level_as_of_overrides_wording(loaded):
    r = run_agent(loaded, RulePlanner(), "What is ACME FY2025 net income?", as_of=date(2026, 4, 1))
    assert "$18.4 million" in r.answer


def test_thousands_unit_answer(loaded):
    r = run_agent(loaded, RulePlanner(), "What was Globex revenue in fiscal 2025?")
    assert "$482.35 million" in r.answer


def test_abstains_before_first_filing(loaded):
    r = run_agent(loaded, RulePlanner(), "ACME FY2025 revenue as of 2026-01-31?")
    assert r.abstained and r.reason == "no_evidence" and r.citations == []


def test_abstains_on_unknown_company(loaded):
    r = run_agent(loaded, RulePlanner(), "What was Umbrella Corp revenue for FY2025?")
    assert r.abstained and r.reason.startswith("unresolved_company")


def test_abstains_on_missing_period(loaded):
    r = run_agent(loaded, RulePlanner(), "What was ACME revenue for FY2019?")
    assert r.abstained and r.reason == "no_evidence"


def test_history_answer_passes_gate_regression(loaded):
    # Regression: the first eval run abstained here because the answer said
    # "2 published versions" and the gate (correctly) rejected the bare "2".
    r = run_agent(loaded, RulePlanner(), "Was ACME's FY2025 revenue restated?")
    assert not r.abstained, r.steps
    assert "two published versions" in r.answer
    assert [c["status"] for c in r.citations] == ["original", "restated"]


def test_as_of_from_question_is_enforced_by_gate(loaded):
    r = run_agent(loaded, RulePlanner(), "ACME FY2025 revenue as of 2026-03-01?")
    assert r.as_of == "2026-03-01"
    assert all(c["filed_at"] <= "2026-03-01" for c in r.citations)
