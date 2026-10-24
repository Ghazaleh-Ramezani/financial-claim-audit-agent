from sqlalchemy import func, select

from app.database import claims, filings
from app.ingest import ingest_directory

from .conftest import FILINGS


def _counts(engine):
    with engine.connect() as conn:
        return (conn.execute(select(func.count()).select_from(filings)).scalar_one(),
                conn.execute(select(func.count()).select_from(claims)).scalar_one())


def test_first_ingest_counts_and_redownload_is_detected(engine):
    report = ingest_directory(engine, FILINGS)
    assert report.inserted_filings == 5
    assert report.inserted_claims == 10
    # acme_10k_fy2025_redownload.json is a byte-identical copy of the original 10-K
    assert [p.split("/")[-1] for p in report.skipped_duplicates] == ["acme_10k_fy2025_redownload.json"]
    assert _counts(engine) == (5, 10)


def test_reingesting_everything_changes_nothing(engine):
    ingest_directory(engine, FILINGS)
    before = _counts(engine)
    report = ingest_directory(engine, FILINGS)
    assert report.inserted_filings == 0 and report.inserted_claims == 0
    assert len(report.skipped_duplicates) == 6
    assert _counts(engine) == before
