"""Ingestion and validation of filing documents.

Usage::

    python -m app.ingest fixtures/filings

Each document is validated (schema, units, sign rules, amendment ordering)
before anything is written. A document is written in one transaction, so a
validation failure halfway through never leaves partial claims behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from .database import claims, companies, filings, init_db, make_engine

log = logging.getLogger("ingest")

# unit -> (normalised unit, multiplier)
UNITS: dict[str, tuple[str, Decimal]] = {
    "USD": ("USD", Decimal(1)),
    "USD_thousands": ("USD", Decimal(1_000)),
    "USD_millions": ("USD", Decimal(1_000_000)),
    "USD_billions": ("USD", Decimal(1_000_000_000)),
    "USD_per_share": ("USD_per_share", Decimal(1)),
}

# metric -> (expected normalised unit, may be negative)
METRICS: dict[str, tuple[str, bool]] = {
    "revenue": ("USD", False),
    "operating_income": ("USD", True),
    "net_income": ("USD", True),
    "eps_diluted": ("USD_per_share", True),
}

FORM_TYPES = {"10-K", "10-K/A", "10-Q", "10-Q/A"}


class ValidationError(ValueError):
    pass


@dataclass
class IngestReport:
    inserted_filings: int = 0
    inserted_claims: int = 0
    skipped_duplicates: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


def canonical_sha256(doc: dict[str, Any]) -> str:
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _parse_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{name} is missing or not a string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} is not an ISO date: {value!r}") from exc


def validate(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised document or raise ValidationError. Pure function."""
    for key in ("accession", "company", "form_type", "facts"):
        if key not in doc:
            raise ValidationError(f"missing field: {key}")
    filed_at = _parse_date(doc.get("filed_at"), "filed_at")
    if doc["form_type"] not in FORM_TYPES:
        raise ValidationError(f"unsupported form_type {doc['form_type']!r}")
    is_amendment = doc["form_type"].endswith("/A")
    if is_amendment and not doc.get("amends"):
        raise ValidationError("amendment without 'amends' accession")
    company = doc["company"]
    if not company.get("ticker") or not company.get("name"):
        raise ValidationError("company.ticker and company.name are required")

    facts = []
    seen: set[tuple[str, str]] = set()
    for i, fact in enumerate(doc["facts"]):
        metric = fact.get("metric")
        if metric not in METRICS:
            raise ValidationError(f"fact {i}: unknown metric {metric!r}")
        unit = fact.get("unit")
        if unit not in UNITS:
            raise ValidationError(f"fact {i}: unsupported unit {unit!r}")
        norm_unit, mult = UNITS[unit]
        expected_unit, may_be_negative = METRICS[metric]
        if norm_unit != expected_unit:
            raise ValidationError(f"fact {i}: unit {unit} incompatible with {metric}")
        try:
            raw = Decimal(str(fact.get("value")))
        except (InvalidOperation, TypeError) as exc:
            raise ValidationError(f"fact {i}: value is not a number") from exc
        if not raw.is_finite():
            raise ValidationError(f"fact {i}: value is not finite")
        if raw < 0 and not may_be_negative:
            raise ValidationError(f"fact {i}: {metric} cannot be negative")
        key = (metric, fact.get("fiscal_period"))
        if key in seen:
            raise ValidationError(f"fact {i}: duplicate {key} inside one filing")
        seen.add(key)
        if not fact.get("source_span"):
            raise ValidationError(f"fact {i}: source_span is required for provenance")
        facts.append(
            {
                "metric": metric,
                "fiscal_period": fact["fiscal_period"],
                "period_end": _parse_date(fact.get("period_end"), f"fact {i} period_end"),
                "value": raw * mult,
                "unit": norm_unit,
                "reported_value": str(fact["value"]),
                "reported_unit": unit,
                "source_span": fact["source_span"],
            }
        )
    return {
        "accession": doc["accession"],
        "ticker": company["ticker"].upper(),
        "company_name": company["name"],
        "form_type": doc["form_type"],
        "filed_at": filed_at,
        "amends": doc.get("amends"),
        "narrative": doc.get("narrative"),
        "facts": facts,
    }


def _insert(engine: Engine):
    return pg_insert if engine.dialect.name == "postgresql" else sqlite_insert


def ingest_document(engine: Engine, doc: dict[str, Any], source_uri: str) -> tuple[str, int]:
    """Ingest one document. Returns ("inserted"|"duplicate", n_claims)."""
    norm = validate(doc)
    sha = canonical_sha256(doc)
    insert = _insert(engine)
    with engine.begin() as conn:
        existing = conn.execute(
            select(filings.c.id, filings.c.sha256).where(filings.c.accession == norm["accession"])
        ).first()
        if existing is not None:
            if existing.sha256 == sha:
                return "duplicate", 0
            raise ValidationError(
                f"accession {norm['accession']} already ingested with different content; "
                "filings are immutable, corrections must arrive as a new (amended) filing"
            )
        if norm["amends"]:
            original = conn.execute(
                select(filings.c.filed_at, filings.c.ticker).where(
                    filings.c.accession == norm["amends"]
                )
            ).first()
            if original is None:
                raise ValidationError(f"amends unknown accession {norm['amends']}")
            if original.ticker != norm["ticker"]:
                raise ValidationError("amendment ticker differs from the original filing")
            if norm["filed_at"] < original.filed_at:
                raise ValidationError("amendment is dated before the filing it amends")

        conn.execute(
            insert(companies)
            .values(ticker=norm["ticker"], name=norm["company_name"])
            .on_conflict_do_nothing(index_elements=["ticker"])
        )
        filing_id = conn.execute(
            filings.insert()
            .values(
                accession=norm["accession"],
                ticker=norm["ticker"],
                form_type=norm["form_type"],
                filed_at=norm["filed_at"],
                amends_accession=norm["amends"],
                source_uri=source_uri,
                sha256=sha,
                narrative=norm["narrative"],
            )
            .returning(filings.c.id)
        ).scalar_one()
        for fact in norm["facts"]:
            conn.execute(
                insert(claims)
                .values(filing_id=filing_id, ticker=norm["ticker"], **fact)
                .on_conflict_do_nothing(index_elements=["filing_id", "metric", "fiscal_period"])
            )
    return "inserted", len(norm["facts"])


def ingest_paths(engine: Engine, paths: list[Path]) -> IngestReport:
    """Ingest files in filing-date order so amendments follow their originals."""
    report = IngestReport()
    docs: list[tuple[str, Path, dict[str, Any]]] = []
    for path in paths:
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            report.rejected[str(path)] = f"invalid JSON: {exc}"
            continue
        docs.append((str(doc.get("filed_at", "9999-12-31")), path, doc))
    for _, path, doc in sorted(docs, key=lambda t: (t[0], t[1].name)):
        try:
            status, n = ingest_document(engine, doc, source_uri=path.as_posix())
        except ValidationError as exc:
            report.rejected[str(path)] = str(exc)
            log.warning("rejected %s: %s", path, exc)
            continue
        if status == "duplicate":
            report.skipped_duplicates.append(str(path))
        else:
            report.inserted_filings += 1
            report.inserted_claims += n
    return report


def ingest_directory(engine: Engine, directory: str | Path) -> IngestReport:
    return ingest_paths(engine, sorted(Path(directory).glob("*.json")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="JSON files or directories")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    engine = make_engine()
    init_db(engine)
    files: list[Path] = []
    for p in map(Path, args.paths):
        files.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    report = ingest_paths(engine, files)
    print(json.dumps(report.__dict__, indent=2))
    return 1 if report.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
