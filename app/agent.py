"""Agent loop: planner -> validated tool calls -> verification gate -> answer.

The loop is the same for every planner. It owns the read-only connection, the
step budget, the trace, and the final verification; the planner only proposes.

Answer modes:

* ``model``: the planner's own answer text is released if it passes the gate.
* ``rendered``: the planner chooses the evidence (which claim it cites); the
  answer text is then written from the claim store by ``render_answer``, so the
  model never writes a number. The model's text is kept in the trace.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from . import claims as claim_store
from .database import readonly_connection
from .planners import AgentState, Final, Planner, parse_as_of, parse_intent, parse_metric, parse_period, render_answer
from .tools import ToolExecutor
from .verify import CHECKS, Scope, claim_key, verify_answer

log = logging.getLogger("agent")

MAX_STEPS = 6
ANSWER_MODES = ("model", "rendered")


@dataclass
class AgentResult:
    trace_id: str
    question: str
    as_of: str | None
    planner: str
    answer: str
    abstained: bool
    reason: str | None
    citations: list[dict[str, Any]]
    verification: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self, include_trace: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_trace:
            d.pop("steps")
        return d


def run_agent(
    engine: Engine,
    planner: Planner,
    question: str,
    as_of: date | None = None,
    gate_checks: frozenset[str] = CHECKS,
    answer_mode: str = "model",
) -> AgentResult:
    if answer_mode not in ANSWER_MODES:
        raise ValueError(f"answer_mode must be one of {ANSWER_MODES}")
    trace_id = uuid.uuid4().hex[:16]
    start = time.perf_counter()
    state = AgentState(question=question, as_of=as_of)
    steps: list[dict[str, Any]] = []
    final: Final | None = None

    with readonly_connection(engine) as conn:
        executor = ToolExecutor(conn)
        state.records = executor.records
        while state.step < MAX_STEPS:
            try:
                action = planner.next_action(state)
            except Exception as exc:  # planner/network failure -> abstain, never crash the API
                log.exception("planner error")
                final = Final(f"The planner failed: {type(exc).__name__}", abstain=True, reason="planner_error")
                break
            if isinstance(action, Final):
                final = action
                break
            for call in action:
                rec = executor.call(state.step, call.name, call.arguments)
                steps.append({"step": state.step, "type": "tool_call", "tool": rec.tool,
                              "arguments": rec.arguments, "ok": rec.ok, "error": rec.error,
                              "latency_ms": rec.latency_ms, "result": rec.result})
            state.step += 1
        if final is None:
            final = Final("Step budget exhausted before an answer was produced.", abstain=True,
                          reason="step_budget_exhausted")
        evidence = dict(executor.evidence)
        # The strictest date the run committed to: the request's as_of, a date the
        # question itself states, or any as_of the planner passed to a tool.
        # Citations must respect it, whether or not the planner used it.
        tool_dates = [date.fromisoformat(str(r.arguments["as_of"])) for r in executor.records
                      if r.ok and isinstance(r.arguments, dict) and r.arguments.get("as_of")]
        effective_as_of = min([d for d in [as_of, parse_as_of(question), *tool_dates] if d], default=None)
        scope = question_scope(conn, question)

        if answer_mode == "rendered" and not final.abstain:
            final = _render_from_store(conn, question, final, evidence, effective_as_of, steps, state.step)

        # Current and as-of questions must cite the latest version visible at the
        # effective date; original and history questions may cite earlier ones.
        latest = None
        if not final.abstain and parse_intent(question) == "current":
            latest = {}
            for cid in final.citations:
                if cid in evidence:
                    key = claim_key(evidence[cid])
                    newest = claim_store.claim_as_of(conn, *key, effective_as_of)
                    if newest is not None:
                        latest[key] = newest["claim_id"]

    if final.abstain:
        verification = {"passed": True, "failures": [], "checked_amounts": [], "skipped": "abstention"}
        answer, abstained, reason, cited_ids = final.answer, True, final.reason, []
    else:
        v = verify_answer(final.answer, final.citations, evidence, as_of=effective_as_of,
                          latest=latest, scope=scope, checks=gate_checks)
        verification = asdict(v)
        if v.passed:
            answer, abstained, reason, cited_ids = final.answer, False, None, final.citations
        else:
            answer = "I could not verify an answer against the claim store, so I am not giving one."
            abstained, reason, cited_ids = True, "verification_failed", []
            steps.append({"step": state.step, "type": "verification_rejected",
                          "rejected_answer": final.answer, "failures": v.failures})

    result = AgentResult(
        trace_id=trace_id, question=question,
        as_of=effective_as_of.isoformat() if effective_as_of else None,
        planner=planner.name, answer=answer, abstained=abstained, reason=reason,
        # An id outside the evidence can only get here with the citation check
        # disabled (gate ablation); it is reported as a bare id, never resolved.
        citations=[_citation(evidence.get(c, {"claim_id": c})) for c in dict.fromkeys(cited_ids)],
        verification=verification, steps=steps,
        latency_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    log.info(json.dumps({"event": "agent_run", "trace_id": trace_id, "planner": planner.name,
                         "abstained": abstained, "reason": reason,
                         "tools": [s.get("tool") for s in steps if s["type"] == "tool_call"],
                         "verification_passed": verification["passed"],
                         "latency_ms": result.latency_ms}))
    return result


def question_scope(conn, question: str) -> Scope:
    """Companies, metric and fiscal period the question names (only those it names)."""
    scope: Scope = {}
    tickers = {c["ticker"] for c in claim_store.list_companies(conn)
               if re.search(rf"\b{re.escape(c['ticker'])}\b", question)
               or c["name"].split()[0].lower() in question.lower()}
    if tickers:
        scope["ticker"] = tickers
    if metric := parse_metric(question):
        scope["metric"] = {metric}
    if period := parse_period(question):
        scope["fiscal_period"] = {period}
    return scope


def _render_from_store(conn, question: str, final: Final, evidence: dict[int, dict[str, Any]],
                       as_of: date | None, steps: list[dict[str, Any]], step: int) -> Final:
    """Rendered answer mode: rewrite the answer from the store for the cited claim."""
    if not final.citations or any(c not in evidence for c in final.citations):
        return final  # left for the gate, which rejects missing or unretrieved citations
    keys = list(dict.fromkeys(claim_key(evidence[c]) for c in final.citations))
    if len(keys) != 1:
        return Final("The answer cites more than one figure; rendered mode answers one figure per question.",
                     abstain=True, reason="render_unsupported")
    intent = "as_of" if as_of else parse_intent(question)
    if intent in ("current", "as_of"):
        result = claim_store.claim_as_of(conn, *keys[0], as_of)
        records = [result] if result else []
    else:
        result = claim_store.claim_history(conn, *keys[0], as_of)
        records = result
    if not records:
        return Final("No filing visible at the requested date reports this figure.",
                     abstain=True, reason="no_evidence")
    for c in records:  # retrieved by the system, so citable
        evidence[c["claim_id"]] = c
    rendered = render_answer(intent, result, as_of)
    steps.append({"step": step, "type": "answer_rendered", "model_answer": final.answer,
                  "rendered_answer": rendered.answer})
    return rendered


def _citation(c: dict[str, Any]) -> dict[str, Any]:
    keys = ("claim_id", "ticker", "metric", "fiscal_period", "value", "unit", "display",
            "accession", "form_type", "filed_at", "source_uri", "status")
    return {k: c.get(k) for k in keys}
