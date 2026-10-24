from app import claims as q
from app.database import readonly_connection


def test_original_value_is_preserved_after_restatement(loaded):
    with readonly_connection(loaded) as conn:
        history = q.claim_history(conn, "ACME", "revenue", "FY2025")
    assert [h["value"] for h in history] == ["120000000", "115000000"]
    first, second = history
    assert first["status"] == "original" and first["accession"] == "SYN-ACME-26-000101"
    assert second["status"] == "restated" and second["accession"] == "SYN-ACME-26-000187"
    assert second["previous_value"] == "120000000"
    assert second["change"] == "-5000000"
    assert second["filed_at"] == "2026-05-10" and second["is_current"]


def test_unchanged_value_in_amendment_is_reaffirmed_not_restated(loaded):
    with readonly_connection(loaded) as conn:
        history = q.claim_history(conn, "ACME", "operating_income", "FY2025")
    assert [h["status"] for h in history] == ["original", "reaffirmed"]


def test_restatement_list(loaded):
    with readonly_connection(loaded) as conn:
        events = q.restatements(conn)
    assert sorted((e["ticker"], e["metric"]) for e in events) == [("ACME", "net_income"), ("ACME", "revenue")]


def test_each_version_keeps_its_own_provenance(loaded):
    with readonly_connection(loaded) as conn:
        history = q.claim_history(conn, "ACME", "net_income", "FY2025")
    assert [h["untrusted_source_text"] for h in history] == [
        "Net income was $18.4 million.", "As restated, net income was $15.9 million."]
    assert history[0]["source_uri"].endswith("acme_10k_fy2025.json")
    assert history[1]["source_uri"].endswith("acme_10ka_fy2025.json")
