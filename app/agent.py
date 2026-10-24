"""Agent loop: planner -> validated tool calls -> verification gate -> answer.

The loop is the same for every planner. It owns the read-only connection, the
step budget, the trace, and the final verification; the planner only proposes.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from .database import readonly_connection
from .planners import AgentState, Final, Planner
from .tools import ToolExecutor
from .verify import verify_answer

log = logging.getLogger("agent")

MAX_STEPS = 6


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


def run_agent(engine: Engine, planner: Planner, question: str, as_of: date | None = None) -> AgentResult:
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
        # The strictest date the run committed to: the request's as_of, or any
        # as_of the planner passed to a tool. Citations must respect it.
        tool_dates = [date.fromisoformat(str(r.arguments["as_of"])) for r in executor.records
                      if r.ok and isinstance(r.arguments, dict) and r.arguments.get("as_of")]
        effective_as_of = min([d for d in [as_of, *tool_dates] if d], default=None)

    if final.abstain:
        verification = {"passed": True, "failures": [], "checked_amounts": [], "skipped": "abstention"}
        answer, abstained, reason, cited_ids = final.answer, True, final.reason, []
    else:
        v = verify_answer(final.answer, final.citations, evidence, as_of=effective_as_of)
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
        citations=[_citation(evidence[c]) for c in dict.fromkeys(cited_ids)],
        verification=verification, steps=steps,
        latency_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    log.info(json.dumps({"event": "agent_run", "trace_id": trace_id, "planner": planner.name,
                         "abstained": abstained, "reason": reason,
                         "tools": [s.get("tool") for s in steps if s["type"] == "tool_call"],
                         "verification_passed": verification["passed"],
                         "latency_ms": result.latency_ms}))
    return result


def _citation(c: dict[str, Any]) -> dict[str, Any]:
    keys = ("claim_id", "ticker", "metric", "fiscal_period", "value", "unit", "display",
            "accession", "form_type", "filed_at", "source_uri", "status")
    return {k: c.get(k) for k in keys}
