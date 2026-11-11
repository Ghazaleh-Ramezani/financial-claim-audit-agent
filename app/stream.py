"""Streaming ingestion from Kafka.

    filings.incoming  ──▶  consumer (validate + ingest, one transaction per filing)
                              ├──▶ claims.ingested   one event per filing, with any restatements it caused
                              └──▶ filings.rejected  dead-letter queue: the reason and the original payload

Delivery is at-least-once: the consumer commits a message's offset only after its result
has been produced and flushed. Redelivery is harmless because ingestion is idempotent
(SHA-256 dedupe per accession), so a replayed filing yields a ``duplicate`` event and no
new claims. Producers key messages by ticker, which keeps a company's filings on one
partition and therefore in order, so an amendment follows the filing it amends.

    python -m app.stream produce fixtures/filings        # publish filings (keyed by ticker)
    python -m app.stream consume --metrics-port 9102     # run the ingestion consumer
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from prometheus_client import Counter, start_http_server

from . import claims as q
from .database import init_db, make_engine, readonly_connection
from .ingest import ValidationError, ingest_document
from .metrics import REGISTRY

log = logging.getLogger("stream")

TOPIC_IN = os.environ.get("FILINGS_TOPIC", "filings.incoming")
TOPIC_EVENTS = os.environ.get("EVENTS_TOPIC", "claims.ingested")
TOPIC_DLQ = os.environ.get("DLQ_TOPIC", "filings.rejected")
MAX_PAYLOAD = 1_000_000

MESSAGES = Counter("stream_messages_total", "Filings consumed from Kafka, by outcome", ["outcome"],
                   registry=REGISTRY)
RESTATEMENTS = Counter("stream_restatements_total", "Restatements detected while ingesting from Kafka",
                       registry=REGISTRY)


class KafkaMessage(Protocol):
    def topic(self) -> str: ...
    def partition(self) -> int: ...
    def offset(self) -> int: ...
    def key(self) -> bytes | None: ...
    def value(self) -> bytes | None: ...
    def error(self) -> Any: ...


@dataclass
class Result:
    topic: str            # TOPIC_EVENTS or TOPIC_DLQ
    key: bytes | None
    event: dict[str, Any]

    @property
    def outcome(self) -> str:
        return self.event.get("outcome", "rejected")


def _restatements(engine, ticker: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restatements this filing caused: the latest version of each reported figure, if restated."""
    found = []
    with readonly_connection(engine) as conn:
        for fact in facts:
            history = q.claim_history(conn, ticker, fact["metric"], fact["fiscal_period"])
            if history and history[-1]["status"] == "restated":
                latest = history[-1]
                found.append({"metric": latest["metric"], "fiscal_period": latest["fiscal_period"],
                              "from_value": latest["previous_value"], "to_value": latest["value"],
                              "change": latest["change"], "claim_id": latest["claim_id"]})
    return found


def process(engine, msg: KafkaMessage) -> Result:
    """Turn one Kafka message into an ingestion event or a dead-letter record.

    A bad document never raises; it becomes a dead-letter record. Database errors do raise,
    so the consumer stops before committing and the message is redelivered after restart.
    """
    uri = f"kafka://{msg.topic()}/{msg.partition()}/{msg.offset()}"
    raw = msg.value() or b""
    try:
        if len(raw) > MAX_PAYLOAD:
            raise ValidationError(f"payload larger than {MAX_PAYLOAD} bytes")
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"invalid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise ValidationError("message must be a JSON object")
        status, n = ingest_document(engine, doc, source_uri=uri)
    except ValidationError as exc:
        reason = str(exc)
        return Result(TOPIC_DLQ, msg.key(), {
            "outcome": "rejected", "source_uri": uri, "reason": reason,
            # An amendment ahead of its original is worth replaying later; other rejections are final.
            "retryable": reason.startswith("amends unknown accession"),
            "payload": raw[:MAX_PAYLOAD].decode("utf-8", errors="replace"),
        })
    ticker = str(doc["company"]["ticker"]).upper()
    event = {"outcome": "ingested" if status == "inserted" else "duplicate", "source_uri": uri,
             "accession": doc["accession"], "ticker": ticker, "form_type": doc["form_type"],
             "filed_at": doc["filed_at"], "claims": n, "restatements": []}
    if status == "inserted":
        event["restatements"] = _restatements(engine, ticker, doc["facts"])
    return Result(TOPIC_EVENTS, msg.key(), event)


def run(engine, consumer, producer, *, max_messages: int | None = None, idle_exit: float | None = None,
        poll_timeout: float = 1.0, should_stop=lambda: False) -> dict[str, int]:
    """Consume, process, produce, then commit. Returns counts by outcome."""
    counts: dict[str, int] = {}
    idle_since = time.monotonic()
    while not should_stop():
        if max_messages is not None and sum(counts.values()) >= max_messages:
            break
        msg = consumer.poll(poll_timeout)
        if msg is None:
            if idle_exit is not None and time.monotonic() - idle_since >= idle_exit:
                break
            continue
        if msg.error():
            log.warning(json.dumps({"event": "kafka_error", "error": str(msg.error())}))
            continue
        idle_since = time.monotonic()
        result = process(engine, msg)
        producer.produce(result.topic, key=result.key, value=json.dumps(result.event).encode())
        producer.flush()                                   # the result is durable ...
        consumer.commit(message=msg, asynchronous=False)   # ... before the input is acknowledged
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        MESSAGES.labels(result.outcome).inc()
        RESTATEMENTS.inc(len(result.event.get("restatements", [])))
        log.info(json.dumps({"event": "stream_message", "outcome": result.outcome,
                             "source_uri": result.event["source_uri"],
                             "accession": result.event.get("accession"), "reason": result.event.get("reason")}))
    return counts


# ---------------------------------------------------------------- Kafka wiring (confluent-kafka)

def kafka_config() -> dict[str, Any]:
    conf = {"bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
    if os.environ.get("KAFKA_SECURITY_PROTOCOL"):          # e.g. SASL_SSL for MSK / Confluent Cloud
        conf["security.protocol"] = os.environ["KAFKA_SECURITY_PROTOCOL"]
    if os.environ.get("KAFKA_SASL_MECHANISM"):
        conf.update({"sasl.mechanism": os.environ["KAFKA_SASL_MECHANISM"],
                     "sasl.username": os.environ.get("KAFKA_SASL_USERNAME", ""),
                     "sasl.password": os.environ.get("KAFKA_SASL_PASSWORD", "")})
    return conf


def ensure_topics(partitions: int = 3) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient(kafka_config())
    existing = set(admin.list_topics(timeout=10).topics)
    new = [NewTopic(t, num_partitions=partitions, replication_factor=1)
           for t in (TOPIC_IN, TOPIC_EVENTS, TOPIC_DLQ) if t not in existing]
    for topic, fut in (admin.create_topics(new).items() if new else []):
        try:
            fut.result()
        except Exception as exc:  # created concurrently by another process
            if "TOPIC_ALREADY_EXISTS" not in str(exc):
                raise
            log.info("topic %s already exists", topic)


def make_consumer(group_id: str = "claim-ingest"):
    from confluent_kafka import Consumer

    consumer = Consumer({**kafka_config(), "group.id": group_id, "enable.auto.commit": False,
                         "auto.offset.reset": "earliest", "isolation.level": "read_committed"})
    consumer.subscribe([TOPIC_IN])
    return consumer


def make_producer():
    from confluent_kafka import Producer

    return Producer({**kafka_config(), "enable.idempotence": True, "acks": "all"})


def produce_files(producer, paths: list[Path]) -> int:
    """Publish filings in filing-date order, keyed by ticker (one partition per company)."""
    docs = []
    for path in paths:
        raw = path.read_bytes()
        try:
            doc = json.loads(raw)
            key, order = str(doc["company"]["ticker"]).upper(), str(doc.get("filed_at", ""))
        except (ValueError, KeyError, TypeError):
            key, order = "unknown", "9999"
        docs.append((order, path.name, key, raw))
    for _, _, key, raw in sorted(docs):
        producer.produce(TOPIC_IN, key=key.encode(), value=raw)
    producer.flush()
    return len(docs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kafka ingestion for the claim store.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("produce", help="publish filing JSON files to the input topic")
    p.add_argument("paths", nargs="+")
    c = sub.add_parser("consume", help="run the ingestion consumer")
    c.add_argument("--max-messages", type=int)
    c.add_argument("--idle-exit", type=float, help="stop after this many seconds without messages")
    c.add_argument("--metrics-port", type=int, help="serve Prometheus metrics on this port")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ensure_topics()

    if args.command == "produce":
        files: list[Path] = []
        for path in map(Path, args.paths):
            files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        print(json.dumps({"produced": produce_files(make_producer(), files), "topic": TOPIC_IN}))
        return 0

    if args.metrics_port:
        start_http_server(args.metrics_port, registry=REGISTRY)
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(flag=True))
    engine = make_engine()
    init_db(engine)
    consumer, producer = make_consumer(), make_producer()
    try:
        counts = run(engine, consumer, producer, max_messages=args.max_messages, idle_exit=args.idle_exit,
                     should_stop=lambda: stop["flag"])
    finally:
        consumer.close()
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
