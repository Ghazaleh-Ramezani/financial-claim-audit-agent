"""HTTP API. Read-only: ingestion is a separate CLI step (``python -m app.ingest``).

Operational endpoints: ``/health`` (liveness: the database answers), ``/ready``
(readiness: the claim store is loaded), ``/metrics`` (Prometheus). Every response
carries an ``X-Request-ID`` (echoed when the caller sends a valid one) and every
request is logged as one JSON line.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from . import claims as q
from . import metrics
from .agent import run_agent
from .database import claims, filings, init_db, make_engine, readonly_connection
from .multiagent import system_from_env
from .planners import planner_from_env
from .tools import Metric

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
access_log = logging.getLogger("access")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    as_of: date | None = Field(default=None, description="Exclude filings published after this date")
    include_trace: bool = True
    answer_mode: Literal["model", "rendered"] = Field(
        default="model", description="rendered: the answer text is written from the cited claim, not by the model")
    architecture: Literal["single", "multi"] = Field(
        default="single", description="multi: retriever, analyst and writer agents with a verifier (app/multiagent.py)")


def create_app(engine=None, planner=None, multi=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.engine)
        yield

    app = FastAPI(title="Financial Claim Audit Agent", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine or make_engine()
    app.state.planner = planner or planner_from_env()
    app.state.multi = multi or system_from_env()

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if REQUEST_ID.match(rid) else uuid.uuid4().hex[:16]
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        access_log.info(json.dumps({"event": "http_request", "request_id": rid, "method": request.method,
                                    "path": request.url.path, "status": response.status_code,
                                    "latency_ms": round((time.perf_counter() - start) * 1000, 2)}))
        return response

    @app.get("/health")
    def health() -> dict[str, Any]:
        with readonly_connection(app.state.engine) as conn:
            conn.execute(text("SELECT 1"))
            n_filings = conn.execute(select(func.count()).select_from(filings)).scalar_one()
            n_claims = conn.execute(select(func.count()).select_from(claims)).scalar_one()
        return {"status": "ok", "database": app.state.engine.dialect.name,
                "planner": app.state.planner.name, "multi_agent": app.state.multi.name,
                "filings": n_filings, "claims": n_claims}

    @app.get("/ready")
    def ready(response: Response) -> dict[str, Any]:
        try:
            with readonly_connection(app.state.engine) as conn:
                n_claims = conn.execute(select(func.count()).select_from(claims)).scalar_one()
        except Exception as exc:  # database unreachable or schema missing
            response.status_code = 503
            return {"ready": False, "reason": type(exc).__name__}
        if n_claims == 0:
            response.status_code = 503
            return {"ready": False, "reason": "claim store is empty; run python -m app.ingest"}
        return {"ready": True, "claims": n_claims}

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.post("/ask")
    def ask(req: AskRequest, response: Response) -> dict[str, Any]:
        if req.architecture == "multi":
            result = app.state.multi.run(app.state.engine, req.question, req.as_of)
        else:
            result = run_agent(app.state.engine, app.state.planner, req.question, req.as_of,
                               answer_mode=req.answer_mode)
        metrics.record(result)
        response.headers["X-Trace-ID"] = result.trace_id
        return result.to_dict(include_trace=req.include_trace)

    @app.get("/companies")
    def companies() -> list[dict[str, str]]:
        with readonly_connection(app.state.engine) as conn:
            return q.list_companies(conn)

    @app.get("/claims/{ticker}/{metric}/{fiscal_period}")
    def claim(ticker: str, metric: Metric, fiscal_period: str,
              as_of: date | None = Query(default=None)) -> dict[str, Any]:
        with readonly_connection(app.state.engine) as conn:
            c = q.claim_as_of(conn, ticker, metric, fiscal_period, as_of)
        if c is None:
            raise HTTPException(404, "no claim published for this key" + (f" on or before {as_of}" if as_of else ""))
        return c

    @app.get("/claims/{ticker}/{metric}/{fiscal_period}/history")
    def history(ticker: str, metric: Metric, fiscal_period: str) -> list[dict[str, Any]]:
        with readonly_connection(app.state.engine) as conn:
            h = q.claim_history(conn, ticker, metric, fiscal_period)
        if not h:
            raise HTTPException(404, "no claim for this key")
        return h

    @app.get("/restatements")
    def restatements(ticker: str | None = None) -> list[dict[str, Any]]:
        with readonly_connection(app.state.engine) as conn:
            return q.restatements(conn, ticker)

    return app


app = create_app()
