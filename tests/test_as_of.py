from datetime import date

import pytest

from app import claims as q
from app.database import readonly_connection


@pytest.mark.parametrize("as_of, expected", [
    (date(2026, 2, 14), None),                 # day before the 10-K: nothing published yet
    (date(2026, 2, 15), "120000000"),          # filing date is inclusive
    (date(2026, 5, 9), "120000000"),           # day before the 10-K/A
    (date(2026, 5, 10), "115000000"),
    (None, "115000000"),
])
def test_as_of_never_returns_later_filings(loaded, as_of, expected):
    with readonly_connection(loaded) as conn:
        c = q.claim_as_of(conn, "ACME", "revenue", "FY2025", as_of)
    assert (c["value"] if c else None) == expected
    if c and as_of:
        assert date.fromisoformat(c["filed_at"]) <= as_of


def test_as_of_history_is_truncated(loaded):
    with readonly_connection(loaded) as conn:
        h = q.claim_history(conn, "ACME", "revenue", "FY2025", as_of=date(2026, 3, 1))
    assert len(h) == 1 and h[0]["is_current"]
