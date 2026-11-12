"""S3 -> Step Functions -> Lambda ingestion, against moto's S3 and Secrets Manager."""

import json

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import func, select

from app import aws_ingest
from app.database import claims
from app.ingest import ValidationError

from .conftest import FILINGS, INVALID

BUCKET = "claim-audit-filings-test"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="ca-central-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


def upload(s3, path, key=None):
    key = key or f"incoming/{path.name}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=path.read_bytes())
    return key


def eventbridge_event(key):
    """Shape of the EventBridge 'Object Created' event that starts the state machine."""
    return {"detail-type": "Object Created", "source": "aws.s3",
            "detail": {"bucket": {"name": BUCKET}, "object": {"key": key}}}


def test_validate_step_reads_s3_and_returns_summary(s3):
    key = upload(s3, FILINGS / "acme_10ka_fy2025.json")
    out = aws_ingest.handler({"action": "validate", "input": eventbridge_event(key)})
    assert out["accession"] == "SYN-ACME-26-000187"
    assert out["facts"] == 3 and out["bucket"] == BUCKET and out["key"] == key
    assert out["version_id"]


def test_validate_step_raises_validation_error_for_bad_document(s3):
    key = upload(s3, INVALID / "negative_revenue.json")
    with pytest.raises(ValidationError):
        aws_ingest.handler({"action": "validate", "input": eventbridge_event(key)})


def test_non_json_object_is_a_validation_error(s3):
    s3.put_object(Bucket=BUCKET, Key="incoming/broken.json", Body=b"{not json")
    with pytest.raises(ValidationError):
        aws_ingest.validate_object(eventbridge_event("incoming/broken.json"))


def test_ingest_step_writes_claims_with_versioned_s3_lineage(s3, engine):
    keys = [upload(s3, FILINGS / name) for name in ("acme_10k_fy2025.json", "acme_10ka_fy2025.json")]
    for key in keys:
        out = aws_ingest.ingest_object({"bucket": BUCKET, "key": key}, eng=engine)
        assert out["status"] == "inserted"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 6
        uris = [r[0] for r in conn.exec_driver_sql("SELECT source_uri FROM filings")]
    assert all(u.startswith(f"s3://{BUCKET}/incoming/") and "?versionId=" in u for u in uris)


def test_redelivered_event_is_idempotent(s3, engine):
    key = upload(s3, FILINGS / "acme_10k_fy2025.json")
    first = aws_ingest.ingest_object({"bucket": BUCKET, "key": key}, eng=engine)
    second = aws_ingest.ingest_object({"bucket": BUCKET, "key": key}, eng=engine)
    assert (first["status"], second["status"]) == ("inserted", "duplicate")
    assert second["claims"] == 0


def test_quarantine_step_copies_object_and_records_reason(s3):
    key = upload(s3, INVALID / "bad_unit.json")
    cause = json.dumps({"errorType": "ValidationError", "errorMessage": "fact 0: unsupported unit 'EUR'"})
    out = aws_ingest.handler({"action": "quarantine",
                              "input": {"bucket": BUCKET, "key": key,
                                        "error": {"Error": "ValidationError", "Cause": cause}}})
    assert out["quarantined_as"] == "rejected/bad_unit.json"
    reason = json.loads(s3.get_object(Bucket=BUCKET, Key="rejected/bad_unit.json.error.json")["Body"].read())
    assert reason == {"source_key": key, "reason": "fact 0: unsupported unit 'EUR'"}
    assert s3.get_object(Bucket=BUCKET, Key="rejected/bad_unit.json")["Body"].read() == \
        (INVALID / "bad_unit.json").read_bytes()


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        aws_ingest.handler({"action": "delete_everything"})


def test_db_credentials_come_from_secrets_manager(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="ca-central-1")
        arn = sm.create_secret(Name="rds!db-test",
                               SecretString=json.dumps({"username": "claims", "password": "s3cr3t"}))["ARN"]
        monkeypatch.setenv("DB_SECRET_ARN", arn)
        monkeypatch.setenv("DB_HOST", "db.internal")
        aws_ingest._load_db_secret()
    from app.database import database_url

    url = database_url()
    assert url.startswith("postgresql+psycopg://claims:s3cr3t@db.internal:5432/claims")
    monkeypatch.delenv("DB_USER")
    monkeypatch.delenv("DB_PASSWORD")


def test_database_url_prefers_explicit_url(monkeypatch):
    from app.database import DEFAULT_DATABASE_URL, database_url

    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() == DEFAULT_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    monkeypatch.setenv("DB_HOST", "ignored")
    assert database_url() == "sqlite:///x.db"


def test_amendment_before_its_original_is_retryable_not_rejected(s3, engine):
    """Parallel uploads can deliver the 10-K/A first; the state machine retries DependencyNotReady."""
    amendment = upload(s3, FILINGS / "acme_10ka_fy2025.json")
    original = upload(s3, FILINGS / "acme_10k_fy2025.json")
    with pytest.raises(aws_ingest.DependencyNotReady):
        aws_ingest.ingest_object({"bucket": BUCKET, "key": amendment}, eng=engine)
    assert aws_ingest.ingest_object({"bucket": BUCKET, "key": original}, eng=engine)["status"] == "inserted"
    assert aws_ingest.ingest_object({"bucket": BUCKET, "key": amendment}, eng=engine)["status"] == "inserted"
