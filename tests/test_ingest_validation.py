import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database import claims, filings
from app.ingest import ValidationError, ingest_document, ingest_paths, validate

from .conftest import FILINGS, INVALID


def _doc(name):
    return json.loads((FILINGS / name).read_text())


def test_units_are_normalised_to_base_usd():
    norm = validate(_doc("globex_10k_fy2025.json"))
    revenue = next(f for f in norm["facts"] if f["metric"] == "revenue")
    assert revenue["value"] == Decimal("482350000")
    assert (revenue["reported_value"], revenue["reported_unit"]) == ("482350", "USD_thousands")


def test_negative_allowed_for_net_income_only():
    norm = validate(_doc("globex_10k_fy2025.json"))
    assert next(f for f in norm["facts"] if f["metric"] == "net_income")["value"] == Decimal("-12100000")


@pytest.mark.parametrize("name, message", [
    ("bad_unit.json", "unsupported unit"),
    ("missing_filed_at.json", "filed_at"),
    ("negative_revenue.json", "cannot be negative"),
])
def test_invalid_documents_rejected_by_validation(name, message):
    with pytest.raises(ValidationError, match=message):
        validate(json.loads((INVALID / name).read_text()))


def test_amendment_dated_before_original_is_rejected(loaded):
    doc = json.loads((INVALID / "amendment_before_original.json").read_text())
    with pytest.raises(ValidationError, match="before the filing it amends"):
        ingest_document(loaded, doc, "fixtures/invalid/amendment_before_original.json")


def test_same_accession_with_different_content_is_rejected(loaded):
    doc = json.loads((INVALID / "accession_conflict.json").read_text())
    with pytest.raises(ValidationError, match="filings are immutable"):
        ingest_document(loaded, doc, "x")


def test_rejected_document_writes_nothing(engine):
    paths = [p for p in sorted(INVALID.glob("*.json")) if p.name != "accession_conflict.json"]
    report = ingest_paths(engine, paths)
    assert len(report.rejected) == 4
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(filings)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 0


def test_amendment_of_unknown_filing_is_rejected(engine):
    with pytest.raises(ValidationError, match="amends unknown accession"):
        ingest_document(engine, _doc("acme_10ka_fy2025.json"), "x")
