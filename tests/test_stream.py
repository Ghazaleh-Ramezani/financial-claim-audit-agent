"""Kafka ingestion: processing, ordering of produce/commit, dead-lettering, and a live-broker test."""

import json
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import stream
from app.database import claims

from .conftest import FILINGS, INVALID


class Msg:
    def __init__(self, value, offset, key=b"ACME", topic="filings.incoming", partition=0, error=None):
        self._v, self._o, self._k, self._t, self._p, self._e = value, offset, key, topic, partition, error

    def value(self):
        return self._v

    def offset(self):
        return self._o

    def key(self):
        return self._k

    def topic(self):
        return self._t

    def partition(self):
        return self._p

    def error(self):
        return self._e


class FakeConsumer:
    def __init__(self, messages, log):
        self.messages, self.log = list(messages), log

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message, asynchronous):
        assert asynchronous is False
        self.log.append(("commit", message.offset()))


class FakeProducer:
    def __init__(self, log):
        self.log, self.sent, self.pending = log, [], []

    def produce(self, topic, key=None, value=None):
        self.pending.append((topic, key, json.loads(value)))

    def flush(self):
        for item in self.pending:
            self.sent.append(item)
            self.log.append(("flushed", item[0]))
        self.pending.clear()


def fixture_messages():
    names = ["acme_10k_fy2025.json", "globex_10k_fy2025.json", "initech_10k_fy2025.json",
             "acme_10q_q1_2026.json", "acme_10ka_fy2025.json", "acme_10k_fy2025_redownload.json"]
    return [Msg((FILINGS / n).read_bytes(), i) for i, n in enumerate(names)]


def run(engine, messages):
    log = []
    consumer, producer = FakeConsumer(messages, log), FakeProducer(log)
    counts = stream.run(engine, consumer, producer, idle_exit=0, poll_timeout=0)
    return counts, producer.sent, log


def test_stream_ingests_fixtures_and_reports_restatements(engine):
    counts, sent, _ = run(engine, fixture_messages())
    assert counts == {"ingested": 5, "duplicate": 1}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 10
    events = [e for t, _, e in sent if t == stream.TOPIC_EVENTS]
    amendment = next(e for e in events if e["accession"] == "SYN-ACME-26-000187")
    changes = {r["metric"]: Decimal(r["change"]) for r in amendment["restatements"]}
    assert set(changes) == {"revenue", "net_income"}            # operating income was reaffirmed
    assert changes["revenue"] == Decimal(-5_000_000) and changes["net_income"] < 0
    assert all(e["source_uri"].startswith("kafka://filings.incoming/0/") for e in events)


def test_offset_is_committed_only_after_the_result_is_flushed(engine):
    _, _, log = run(engine, fixture_messages()[:3])
    assert log == [("flushed", stream.TOPIC_EVENTS), ("commit", 0),
                   ("flushed", stream.TOPIC_EVENTS), ("commit", 1),
                   ("flushed", stream.TOPIC_EVENTS), ("commit", 2)]


def test_replayed_messages_create_no_new_claims(engine):
    run(engine, fixture_messages())
    counts, _, _ = run(engine, fixture_messages())        # consumer restarted from offset 0
    assert counts == {"duplicate": 6}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 10


def test_invalid_and_malformed_messages_go_to_the_dead_letter_topic(engine):
    bad = [Msg((INVALID / "negative_revenue.json").read_bytes(), 0),
           Msg(b"{not json", 1),
           Msg(b"[1, 2, 3]", 2),
           Msg((FILINGS / "acme_10ka_fy2025.json").read_bytes(), 3)]   # amendment before its original
    counts, sent, _ = run(engine, bad)
    assert counts == {"rejected": 4}
    dlq = [e for t, _, e in sent if t == stream.TOPIC_DLQ]
    assert [e["retryable"] for e in dlq] == [False, False, False, True]
    assert "cannot be negative" in dlq[0]["reason"] and "invalid JSON" in dlq[1]["reason"]
    assert dlq[1]["payload"] == "{not json"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 0


def test_broker_errors_are_skipped_not_processed(engine):
    counts, sent, log = run(engine, [Msg(None, 0, error="_PARTITION_EOF")])
    assert counts == {} and sent == [] and log == []


def test_stream_metrics_are_exported(engine):
    from app.metrics import render

    run(engine, fixture_messages()[:1])
    text = render()[0].decode()
    assert 'stream_messages_total{outcome="ingested"}' in text
    assert "stream_restatements_total" in text


@pytest.mark.skipif(not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"), reason="set KAFKA_BOOTSTRAP_SERVERS for a live broker")
def test_live_kafka_round_trip(engine, monkeypatch):
    from confluent_kafka import Consumer

    suffix = uuid.uuid4().hex[:8]
    for name, var in (("in", "TOPIC_IN"), ("events", "TOPIC_EVENTS"), ("dlq", "TOPIC_DLQ")):
        monkeypatch.setattr(stream, var, f"test.{name}.{suffix}")
    stream.ensure_topics(partitions=3)
    producer = stream.make_producer()
    paths = sorted(FILINGS.glob("*.json")) + [INVALID / "bad_unit.json"]
    assert stream.produce_files(producer, paths) == 7

    consumer = stream.make_consumer(group_id=f"test-{suffix}")
    try:
        counts = stream.run(engine, consumer, producer, max_messages=7, idle_exit=30)
    finally:
        consumer.close()
    assert counts == {"ingested": 5, "duplicate": 1, "rejected": 1}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(claims)).scalar_one() == 10

    reader = Consumer({**stream.kafka_config(), "group.id": f"reader-{suffix}", "auto.offset.reset": "earliest"})
    reader.subscribe([stream.TOPIC_EVENTS, stream.TOPIC_DLQ])
    seen = {stream.TOPIC_EVENTS: 0, stream.TOPIC_DLQ: 0}
    try:
        for _ in range(60):
            msg = reader.poll(1.0)
            if msg is not None and not msg.error():
                seen[msg.topic()] += 1
            if seen == {stream.TOPIC_EVENTS: 6, stream.TOPIC_DLQ: 1}:
                break
    finally:
        reader.close()
    assert seen == {stream.TOPIC_EVENTS: 6, stream.TOPIC_DLQ: 1}
