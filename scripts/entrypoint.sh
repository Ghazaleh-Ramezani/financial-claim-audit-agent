#!/bin/sh
set -e
# Ingestion is idempotent, so running it on every start is safe.
if [ "${INGEST_ON_START}" = "1" ]; then
  python -m app.ingest fixtures/filings
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
