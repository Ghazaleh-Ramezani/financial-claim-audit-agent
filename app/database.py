"""Schema and connection handling.

Design rules (enforced by the schema, not only by application code):

* Filings are immutable. One row per accession number; the SHA-256 of the
  canonical document is stored so a re-download is recognised and a silently
  edited document with the same accession is rejected.
* Claims are append-only. A restatement is a *new* claim attached to a *new*
  filing; the original claim is never updated or deleted.
* The agent and the read API use a read-only connection (``readonly_connection``),
  so even a compromised planner cannot write to the database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import (
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Connection, Engine

DEFAULT_DATABASE_URL = "sqlite:///./claims.db"

metadata = MetaData()

companies = Table(
    "companies",
    metadata,
    Column("ticker", String(12), primary_key=True),
    Column("name", String(200), nullable=False),
)

filings = Table(
    "filings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("accession", String(64), nullable=False, unique=True),
    Column("ticker", String(12), ForeignKey("companies.ticker"), nullable=False),
    Column("form_type", String(16), nullable=False),
    Column("filed_at", Date, nullable=False),
    Column("amends_accession", String(64), ForeignKey("filings.accession"), nullable=True),
    Column("source_uri", String(500), nullable=False),
    Column("sha256", String(64), nullable=False),
    # Free text from the document. Treated as untrusted data everywhere.
    Column("narrative", Text, nullable=True),
)

claims = Table(
    "claims",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filing_id", Integer, ForeignKey("filings.id"), nullable=False),
    Column("ticker", String(12), ForeignKey("companies.ticker"), nullable=False),
    Column("metric", String(64), nullable=False),
    Column("fiscal_period", String(16), nullable=False),
    Column("period_end", Date, nullable=False),
    # Normalised value in base units (USD, or USD per share). Exact decimal.
    Column("value", Numeric(24, 4), nullable=False),
    Column("unit", String(24), nullable=False),
    # What the document actually said, kept for audit.
    Column("reported_value", String(40), nullable=False),
    Column("reported_unit", String(24), nullable=False),
    Column("source_span", Text, nullable=False),
    UniqueConstraint("filing_id", "metric", "fiscal_period", name="uq_claim_per_filing"),
)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    kwargs = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def reset_db(engine: Engine) -> None:
    metadata.drop_all(engine)
    metadata.create_all(engine)


@contextmanager
def readonly_connection(engine: Engine) -> Iterator[Connection]:
    """Connection on which any INSERT/UPDATE/DELETE fails at the database level."""
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
        elif engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            if engine.dialect.name == "sqlite":
                conn.rollback()
                conn.exec_driver_sql("PRAGMA query_only = OFF")
            else:
                conn.rollback()
