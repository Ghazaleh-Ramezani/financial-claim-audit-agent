from decimal import Decimal

from app.verify import extract_amounts, verify_answer

EVIDENCE = {4: {"claim_id": 4, "value": "115000000", "previous_value": "120000000",
                "change": "-5000000", "filed_at": "2026-05-10"}}


def test_extracts_scaled_amounts_and_ignores_dates_and_ids():
    text = "FY2025 revenue was $115.0 million (10-K/A SYN-ACME-26-000187, filed 2026-05-10), down $5 million."
    assert [a.value for a in extract_amounts(text)] == [Decimal("115000000"), Decimal("5000000")]


def test_rounding_tolerance_follows_displayed_precision():
    ev = {1: {"claim_id": 1, "value": "482350000", "filed_at": "2026-03-02"}}
    assert verify_answer("Revenue was $482.4 million.", [1], ev).passed
    assert not verify_answer("Revenue was $483 million.", [1], ev).passed


def test_plain_and_comma_numbers():
    assert verify_answer("Revenue: 115,000,000 USD, previously 120,000,000.", [4], EVIDENCE).passed


def test_rejects_uncited_number():
    v = verify_answer("Revenue was $116 million.", [4], EVIDENCE)
    assert not v.passed and "does not match" in v.failures[0]
