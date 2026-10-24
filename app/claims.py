"""Read-side queries over versioned claims.

Restatement status is derived at query time from the ordered version history,
so the result does not depend on the order in which files were ingested.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from .database import claims, companies, filings


def format_money(value: Decimal, unit: str) -> str:
    if unit == "USD_per_share":
        return f"${value:,.2f} per share"
    sign = "-" if value < 0 else ""
    v = abs(value)
    for size, word in ((Decimal(10) ** 9, "billion"), (Decimal(10) ** 6, "million")):
        if v >= size:
            return f"{sign}${dec_str(v / size)} {word}"
    return f"{sign}${v.quantize(Decimal(1)):,}"


def dec_str(value: Decimal) -> str:
    """Plain decimal string without exponent: Decimal('1.2E+8') -> '120000000'."""
    return format(Decimal(value).normalize(), "f")


def _row_to_claim(row: Any) -> dict[str, Any]:
    value = Decimal(row.value)
    return {
        "claim_id": row.id,
        "ticker": row.ticker,
        "metric": row.metric,
        "fiscal_period": row.fiscal_period,
        "period_end": row.period_end.isoformat(),
        "value": dec_str(value),
        "unit": row.unit,
        "display": format_money(value, row.unit),
        "reported_value": row.reported_value,
        "reported_unit": row.reported_unit,
        "accession": row.accession,
        "form_type": row.form_type,
        "filed_at": row.filed_at.isoformat(),
        "source_uri": row.source_uri,
        # Text copied from the document. Never interpreted as instructions.
        "untrusted_source_text": row.source_span,
    }


def _base_query():
    return select(
        claims.c.id,
        claims.c.ticker,
        claims.c.metric,
        claims.c.fiscal_period,
        claims.c.period_end,
        claims.c.value,
        claims.c.unit,
        claims.c.reported_value,
        claims.c.reported_unit,
        claims.c.source_span,
        filings.c.accession,
        filings.c.form_type,
        filings.c.filed_at,
        filings.c.source_uri,
    ).join(filings, filings.c.id == claims.c.filing_id)


def list_companies(conn: Connection) -> list[dict[str, str]]:
    rows = conn.execute(select(companies).order_by(companies.c.ticker)).all()
    return [{"ticker": r.ticker, "name": r.name} for r in rows]


def available_periods(conn: Connection, ticker: str) -> list[dict[str, str]]:
    rows = conn.execute(
        select(claims.c.metric, claims.c.fiscal_period)
        .where(claims.c.ticker == ticker.upper())
        .distinct()
        .order_by(claims.c.metric, claims.c.fiscal_period)
    ).all()
    return [{"metric": r.metric, "fiscal_period": r.fiscal_period} for r in rows]


def claim_history(
    conn: Connection,
    ticker: str,
    metric: str,
    fiscal_period: str,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """All published versions, oldest first, each annotated with its status."""
    cond = and_(
        claims.c.ticker == ticker.upper(),
        claims.c.metric == metric,
        claims.c.fiscal_period == fiscal_period,
    )
    if as_of is not None:
        cond = and_(cond, filings.c.filed_at <= as_of)
    rows = conn.execute(
        _base_query().where(cond).order_by(filings.c.filed_at, claims.c.id)
    ).all()
    history: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for i, row in enumerate(rows):
        item = _row_to_claim(row)
        item["version"] = i + 1
        if previous is None:
            item["status"] = "original"
        elif Decimal(item["value"]) != Decimal(previous["value"]):
            item["status"] = "restated"
            item["previous_value"] = previous["value"]
            item["previous_claim_id"] = previous["claim_id"]
            item["change"] = dec_str(Decimal(item["value"]) - Decimal(previous["value"]))
        else:
            item["status"] = "reaffirmed"
        history.append(item)
        previous = item
    for i, item in enumerate(history):
        item["is_current"] = i == len(history) - 1
    return history


def claim_as_of(
    conn: Connection, ticker: str, metric: str, fiscal_period: str, as_of: date | None
) -> dict[str, Any] | None:
    """Latest version published on or before ``as_of`` (None = latest overall)."""
    history = claim_history(conn, ticker, metric, fiscal_period, as_of=as_of)
    return history[-1] if history else None


def original_claim(
    conn: Connection, ticker: str, metric: str, fiscal_period: str
) -> dict[str, Any] | None:
    history = claim_history(conn, ticker, metric, fiscal_period)
    return history[0] if history else None


def restatements(conn: Connection, ticker: str | None = None) -> list[dict[str, Any]]:
    keys_q = select(claims.c.ticker, claims.c.metric, claims.c.fiscal_period).distinct()
    if ticker:
        keys_q = keys_q.where(claims.c.ticker == ticker.upper())
    events = []
    for t, m, p in conn.execute(keys_q.order_by(claims.c.ticker, claims.c.metric)).all():
        for item in claim_history(conn, t, m, p):
            if item["status"] == "restated":
                events.append(item)
    return events
