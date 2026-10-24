"""Shared fixtures.

Tests run against SQLite by default. Set TEST_DATABASE_URL to a PostgreSQL URL
(as CI does) to run the identical suite against PostgreSQL.
"""

import os
from pathlib import Path

import pytest

from app.database import make_engine, reset_db
from app.ingest import ingest_directory

ROOT = Path(__file__).resolve().parents[1]
FILINGS = ROOT / "fixtures" / "filings"
INVALID = ROOT / "fixtures" / "invalid"


@pytest.fixture
def engine(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    eng = make_engine(url)
    reset_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def loaded(engine):
    report = ingest_directory(engine, FILINGS)
    assert not report.rejected, report.rejected
    return engine
