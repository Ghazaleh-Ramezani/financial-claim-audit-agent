"""Event-driven ingestion on AWS: S3 -> EventBridge -> Step Functions -> Lambda.

A filing uploaded to ``s3://<bucket>/incoming/`` emits an EventBridge "Object Created"
event, which starts the ingestion state machine (``deploy/terraform/aws/ingest.tf``).
Each state calls this module's ``handler`` with a different ``action``:

``validate``    read the object and run the same validation as the CLI (``app.ingest.validate``);
                raises ``ValidationError`` for a bad document, which the state machine catches.
``ingest``      write the document in one transaction (``app.ingest.ingest_document``);
                re-delivery of the same object is a no-op (SHA-256 dedupe).
``quarantine``  copy a rejected object to ``rejected/`` with the reason next to it.

The source URI recorded for every claim pins the S3 object *version*, so the lineage of
an answer points at the exact bytes that were ingested even if the key is overwritten.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .ingest import ValidationError, canonical_sha256, ingest_document, validate

log = logging.getLogger("aws_ingest")
log.setLevel(logging.INFO)

_ENGINE = None


def _s3():
    import boto3

    return boto3.client("s3")


def _load_db_secret() -> None:
    """Resolve DB credentials from Secrets Manager (RDS-managed secret) into DB_USER/DB_PASSWORD."""
    arn = os.environ.get("DB_SECRET_ARN")
    if not arn or os.environ.get("DB_PASSWORD"):
        return
    import boto3

    secret = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])
    os.environ["DB_USER"] = secret["username"]
    os.environ["DB_PASSWORD"] = secret["password"]


def engine():
    """One engine per Lambda container, created on first use and reused across invocations."""
    global _ENGINE
    if _ENGINE is None:
        from .database import init_db, make_engine

        _load_db_secret()
        _ENGINE = make_engine()
        init_db(_ENGINE)
    return _ENGINE


def _location(event: dict[str, Any]) -> tuple[str, str]:
    """Bucket and key from either an EventBridge S3 event or a state-machine payload."""
    if "detail" in event:
        return event["detail"]["bucket"]["name"], event["detail"]["object"]["key"]
    return event["bucket"], event["key"]


def _read(bucket: str, key: str) -> tuple[dict[str, Any], str | None]:
    obj = _s3().get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValidationError("document must be a JSON object")
    return doc, obj.get("VersionId")


def source_uri(bucket: str, key: str, version_id: str | None) -> str:
    uri = f"s3://{bucket}/{key}"
    return f"{uri}?versionId={version_id}" if version_id and version_id != "null" else uri


def validate_object(event: dict[str, Any]) -> dict[str, Any]:
    bucket, key = _location(event)
    doc, version_id = _read(bucket, key)
    norm = validate(doc)
    return {
        "bucket": bucket,
        "key": key,
        "version_id": version_id,
        "accession": norm["accession"],
        "ticker": norm["ticker"],
        "filed_at": norm["filed_at"].isoformat(),
        "facts": len(norm["facts"]),
        "sha256": canonical_sha256(doc),
    }


class DependencyNotReady(Exception):
    """An amendment arrived before the filing it amends.

    Uploads are processed in parallel, so a 10-K/A can reach the database before its
    10-K. The state machine retries this error with backoff instead of quarantining the
    document; only if the original never arrives does it end up in ``rejected/``.
    """


def ingest_object(event: dict[str, Any], eng=None) -> dict[str, Any]:
    bucket, key = _location(event)
    doc, version_id = _read(bucket, key)
    try:
        status, n = ingest_document(eng or engine(), doc, source_uri=source_uri(bucket, key, version_id))
    except ValidationError as exc:
        if str(exc).startswith("amends unknown accession"):
            raise DependencyNotReady(str(exc)) from exc
        raise
    result = {"bucket": bucket, "key": key, "accession": doc.get("accession"), "status": status, "claims": n}
    log.info(json.dumps({"event": "s3_ingest", **result}))
    return result


def quarantine_object(event: dict[str, Any]) -> dict[str, Any]:
    """Copy a rejected object to ``rejected/`` and store the reason as a sibling ``.error.json``."""
    bucket, key = _location(event)
    error = event.get("error") or {}
    reason = error.get("Cause") or error.get("Error") or "unknown"
    try:  # Step Functions passes the Lambda error as JSON text in Cause
        reason = json.loads(reason).get("errorMessage", reason)
    except (TypeError, ValueError, AttributeError):
        pass
    name = key.split("/", 1)[1] if key.startswith("incoming/") else key
    target = f"rejected/{name}"
    s3 = _s3()
    s3.copy_object(Bucket=bucket, Key=target, CopySource={"Bucket": bucket, "Key": key})
    s3.put_object(Bucket=bucket, Key=f"{target}.error.json", ContentType="application/json",
                  Body=json.dumps({"source_key": key, "reason": reason}).encode())
    log.warning(json.dumps({"event": "s3_quarantine", "key": key, "target": target, "reason": reason}))
    return {"bucket": bucket, "key": key, "quarantined_as": target, "reason": reason}


ACTIONS = {"validate": validate_object, "ingest": ingest_object, "quarantine": quarantine_object}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point. ``event['action']`` selects the step; default is validate."""
    action = event.get("action", "validate")
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    payload = event.get("input", event)
    return ACTIONS[action](payload)
