"""Tools exposed to the agent.

Every tool is read-only and runs on a read-only database connection. Arguments
are validated with Pydantic before execution; unknown tools and invalid
arguments are returned to the planner as errors and recorded in the trace.
Every claim a tool returns is added to the run's evidence ledger, which the
verification gate later uses to check the final answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import Connection

from . import claims as q
from .ingest import METRICS

Metric = Literal["revenue", "operating_income", "net_income", "eps_diluted"]
assert set(Metric.__args__) == set(METRICS)  # keep tool schema in sync with ingestion


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class FindCompanyArgs(_Args):
    query: str = Field(min_length=1, max_length=100, description="Ticker or company name")


class ClaimArgs(_Args):
    ticker: str = Field(min_length=1, max_length=12)
    metric: Metric
    fiscal_period: str = Field(pattern=r"^(FY\d{4}|Q[1-4]-\d{4})$", description="e.g. FY2025 or Q1-2026")
    as_of: date | None = Field(
        default=None, description="Only use filings published on or before this date"
    )


class OriginalClaimArgs(_Args):
    ticker: str = Field(min_length=1, max_length=12)
    metric: Metric
    fiscal_period: str = Field(pattern=r"^(FY\d{4}|Q[1-4]-\d{4})$")


class RestatementArgs(_Args):
    ticker: str | None = Field(default=None, max_length=12)


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    fn: Callable[[Connection, Any], Any]

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }


def _find_company(conn: Connection, a: FindCompanyArgs):
    needle = a.query.strip().lower()
    return [
        c
        for c in q.list_companies(conn)
        if needle == c["ticker"].lower() or needle in c["name"].lower()
    ]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("list_companies", "List companies in the claim store.", NoArgs,
             lambda c, a: q.list_companies(c)),
        Tool("find_company", "Resolve a ticker or company name to a ticker.", FindCompanyArgs,
             _find_company),
        Tool("get_claim",
             "Current value of a metric for a fiscal period. With as_of, returns the value "
             "that was published on or before that date (no look-ahead).",
             ClaimArgs,
             lambda c, a: q.claim_as_of(c, a.ticker, a.metric, a.fiscal_period, a.as_of)),
        Tool("get_original_claim", "First published value of a metric for a fiscal period.",
             OriginalClaimArgs,
             lambda c, a: q.original_claim(c, a.ticker, a.metric, a.fiscal_period)),
        Tool("get_claim_history",
             "All published versions of a metric, oldest first, with restatement status.",
             ClaimArgs,
             lambda c, a: q.claim_history(c, a.ticker, a.metric, a.fiscal_period, a.as_of)),
        Tool("list_restatements", "All restated claims, optionally for one ticker.",
             RestatementArgs, lambda c, a: q.restatements(c, a.ticker)),
    ]
}


@dataclass
class ToolCallRecord:
    step: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None
    latency_ms: float = 0.0


@dataclass
class ToolExecutor:
    conn: Connection
    records: list[ToolCallRecord] = field(default_factory=list)
    evidence: dict[int, dict[str, Any]] = field(default_factory=dict)

    def _collect(self, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict) and "claim_id" in item:
                self.evidence[item["claim_id"]] = item

    def call(self, step: int, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        start = time.perf_counter()
        tool = TOOLS.get(name)
        if tool is None:
            rec = ToolCallRecord(step, name, arguments, ok=False,
                                 error=f"unknown tool {name!r}; available: {sorted(TOOLS)}")
        else:
            try:
                args = tool.args_model.model_validate(arguments)
                result = tool.fn(self.conn, args)
                self._collect(result)
                rec = ToolCallRecord(step, name, arguments, ok=True, result=result)
            except ValidationError as exc:
                rec = ToolCallRecord(step, name, arguments, ok=False,
                                     error=f"invalid arguments: {exc.errors(include_url=False)}")
        rec.latency_ms = round((time.perf_counter() - start) * 1000, 2)
        self.records.append(rec)
        return rec


def openai_tool_schemas() -> list[dict[str, Any]]:
    return [t.openai_schema() for t in TOOLS.values()]
