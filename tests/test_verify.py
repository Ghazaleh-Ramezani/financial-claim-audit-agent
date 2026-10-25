from decimal import Decimal

from app.verify import amount_role, extract_amounts, verify_answer

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


# ---- role binding and scope (checks 5 and 6) -------------------------------- #

RESTATED = {8: {"claim_id": 8, "ticker": "ACME", "metric": "revenue", "fiscal_period": "FY2025",
                "value": "115000000", "previous_value": "120000000", "change": "-5000000",
                "filed_at": "2026-05-10"}}
LATEST = {("ACME", "revenue", "FY2025"): 8}


def roles(text):
    return [amount_role(a) for a in extract_amounts(text)]


def test_amount_roles_follow_the_words_that_introduce_them():
    assert roles("Revenue is $115 million; the previous figure was $120 million.") == ["headline", "earlier"]
    assert roles("Revenue is $115 million, down $5 million from $120 million.") == ["headline", "change", "earlier"]
    assert roles("ACME cut revenue by $5 million to $115 million.") == ["change", "headline"]


def test_role_check_rejects_a_previous_value_presented_as_current():
    ok = verify_answer("Revenue is $115 million, restated from $120 million.", [8], RESTATED, latest=LATEST)
    swapped = verify_answer("Revenue is $120 million.", [8], RESTATED, latest=LATEST)
    assert ok.passed and not swapped.passed and "headline" in swapped.failures[0]
    # Original and history questions (latest=None) may give either figure.
    assert verify_answer("Revenue was $120 million.", [8], RESTATED).passed


def test_scope_check_rejects_claims_for_another_company():
    v = verify_answer("Revenue is $115 million.", [8], RESTATED, scope={"ticker": {"INIT"}})
    assert not v.passed and "outside the question's scope" in v.failures[0]
    assert verify_answer("Revenue is $115 million.", [8], RESTATED, scope={"ticker": {"ACME"}}).passed


def test_quarter_labels_are_not_read_as_amounts():
    assert extract_amounts("ACME's Q1 2026 revenue was $31.2 million.")[0].text == "$31.2 million"
    assert len(extract_amounts("Q3-2025 and FY 2025 revenue: $10 million")) == 1
