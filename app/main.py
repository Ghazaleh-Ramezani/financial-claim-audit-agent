"""HTTP API. Read-only: ingestion is a separate CLI step (``python -m app.ingest``)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from . import claims as q
from .agent import run_agent
from .database import claims, filings, init_db, make_engine, readonly_connection
from .planners import planner_from_env
from .tools import Metric

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    as_of: date | None = Field(default=None, description="Exclude filings published after this date")
    include_trace: bool = True


def create_app(engine=None, planner=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.engine)
        yield

    app = FastAPI(title="Financial Claim Audit Agent", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine or make_engine()
    app.state.planner = planner or planner_from_env()

    @app.get("/health")
    def health() -> dict[str, Any]:
        with readonly_connection(app.state.engine) as conn:
            conn.execute(text("SELECT 1"))
            n_filings = conn.execute(select(func.count()).select_from(filings)).scalar_one()
            n_claims = conn.execute(select(func.count()).select_from(claims)).scalar_one()
        return {"status": "ok", "database": app.state.engine.dialect.name,
                "planner": app.state.planner.name, "filings": n_filings, "claims": n_claims}

    @app.post("/ask")
    def ask(req: AskRequest) -> dict[str, Any]:
        result = run_agent(app.state.engine, app.state.planner, req.question, req.as_of)
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
