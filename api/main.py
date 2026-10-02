"""x-metrics read API — the dashboard's data, over HTTP, for anything that is not the dashboard.

    GET /health                     database reachable? how old is the data? did the last pipeline attempt finish?
    GET /accounts                   latest measurement per account; filter by tier / min_engagement / status, sort, page
    GET /accounts/{handle}          one account with its full measurement history
    GET /changes                    what changed in the latest run (mart.v_changes); ?run=<id> for an earlier run
    GET /runs                       every run: what it set out to measure and how far it got

Every endpoint except /health needs an `X-API-Key` header (api/auth.py, pg/schema/007):
401 without a valid key, 429 over the key's per-minute limit, every decision logged.

Three rules, each visible in the code:
  1. Read-only by role, not by convention. The connection is a member of xmetrics_reader
     (pg/schema/005); the database refuses writes, whatever the API asks. tests/test_api.py tries.
  2. No SQL is built from input. Every statement is complete in api/queries.py; the sort is a
     whitelist key; everything else is a bind parameter.
  3. The API reads the same views the dashboard reads, so the two cannot disagree — and the
     change report it serves is the one tests/test_pg.py proved equal to the Python one.

Run:  XMETRICS_API_DSN=postgresql://xmetrics_api:...@localhost:5432/xmetrics uvicorn api.main:app --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import os
from typing import Annotated, Literal

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db
from . import queries as q
from .auth import require_key
from .db import get_conn
from .models import Account, AccountDetail, AccountPage, Change, ChangeReport, Health, PipelineAttempt, RunStatus

STALE_AFTER_DAYS = int(os.environ.get("XMETRICS_STALE_AFTER_DAYS", "8"))

app = FastAPI(
    title="x-metrics read API",
    version="0.1.0",
    description=(
        "Read-only access to X account metrics: latest values per account, each account's history, "
        "and what changed in the latest collection run. Same views as the dashboard; a role that cannot write."
    ),
)

Conn = Annotated[psycopg.Connection, Depends(get_conn)]
SortKey = Literal[tuple(q.SORTS)]  # type: ignore[valid-type]  — the whitelist, as the schema shows it


# ---------------------------------------------------------------------------- health --
@app.get("/health", response_model=Health, responses={503: {"description": "database unreachable"}})
def health():
    """Reachability, freshness and the last pipeline attempt. `ok` is false when the newest data
    is older than `stale_after_days` (the dashboard turns red on the same rule), when the last
    scheduled attempt failed (ops.pipeline_runs), or when the database is down."""
    try:
        with db.connect(row_factory=dict_row) as conn:
            runs = conn.execute("SELECT count(*) AS n FROM mart.v_runs").fetchone()["n"]
            row = conn.execute(q.LATEST_RUN).fetchone()
            last = conn.execute(q.LATEST_PIPELINE).fetchone()
    except (psycopg.Error, RuntimeError) as e:
        body = Health(ok=False, database=f"error: {e}", latest_run=None, data_taken_at=None,
                      data_age_seconds=None, stale=True, stale_after_days=STALE_AFTER_DAYS,
                      latest_run_complete=None, runs=0, last_pipeline=None)
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
    pipeline = PipelineAttempt(**last) if last else None
    pipeline_failed = pipeline is not None and pipeline.status == "failed"
    if row is None:
        return Health(ok=False, database="ok", latest_run=None, data_taken_at=None, data_age_seconds=None,
                      stale=True, stale_after_days=STALE_AFTER_DAYS, latest_run_complete=None, runs=runs,
                      last_pipeline=pipeline)
    stale = row["age_seconds"] > STALE_AFTER_DAYS * 86400
    return Health(ok=not stale and not pipeline_failed, database="ok", latest_run=row["run_id"],
                  data_taken_at=row["taken_at"], data_age_seconds=int(row["age_seconds"]), stale=stale,
                  stale_after_days=STALE_AFTER_DAYS, latest_run_complete=row["complete"], runs=runs,
                  last_pipeline=pipeline)


# -------------------------------------------------------------------------- accounts --
@app.get("/accounts", response_model=AccountPage, dependencies=[Depends(require_key)])
def accounts(
    conn: Conn,
    tier: Annotated[Literal[q.TIERS] | None, Query(description="exact tier label")] = None,  # type: ignore[valid-type]
    min_engagement: Annotated[float | None, Query(ge=0, description="engagement_rate >= this (percent)")] = None,
    status: Annotated[Literal["measured", "pending", "screened_out", "error"] | None, Query()] = None,
    sort: Annotated[SortKey, Query(description="prefix '-' for ascending")] = q.DEFAULT_SORT,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Each account's latest measurement (mart.v_latest). `total` counts matches before paging."""
    rows = conn.execute(
        q.ACCOUNTS.format(order_by=q.SORTS[sort]),
        {"tier": tier, "min_engagement": min_engagement, "status": status, "limit": limit, "offset": offset},
    ).fetchall()
    total = rows[0]["total"] if rows else 0
    return AccountPage(total=total, limit=limit, offset=offset, sort=sort,
                       items=[Account(**{k: v for k, v in r.items() if k != "total"}) for r in rows])


@app.get("/accounts/{handle}", response_model=AccountDetail, responses={404: {"description": "unknown handle"}}, dependencies=[Depends(require_key)])
def account(handle: str, conn: Conn):
    """One account, with every measurement it has ever had, oldest first."""
    h = handle.strip().lstrip("@").lower()
    row = conn.execute(q.ACCOUNT, {"handle": h}).fetchone()
    if row is None:
        raise HTTPException(404, f"no account @{h}")
    history = conn.execute(q.HISTORY, {"handle": h}).fetchall()
    return AccountDetail(**row, history=history)


# --------------------------------------------------------------------------- changes --
def _report(rows: list[dict], run: str | None) -> ChangeReport:
    changes = [Change(**r) for r in rows]
    counts = {
        "flagged": sum(1 for c in changes if c.kind == "changed" and c.flags),
        "new": sum(1 for c in changes if c.kind == "new"),
        "dropped": sum(1 for c in changes if c.kind == "dropped"),
        "unchanged": sum(1 for c in changes if c.kind in ("changed", "unchanged") and not c.flags),
        "total": len(changes),
    }
    return ChangeReport(run=run, counts=counts, changes=changes)


@app.get("/changes", response_model=ChangeReport, responses={404: {"description": "unknown run"}}, dependencies=[Depends(require_key)])
def changes(conn: Conn, run: Annotated[str | None, Query(description="a run_id from /runs; default: the latest")] = None):
    """What changed: each account against its own previous measurement. `dropped` only ever
    means the run went looking for the account and it was not there; accounts a run did not try
    are not listed. Empty until two runs have measurements."""
    if run is None:
        latest = conn.execute("SELECT run_id FROM mart.v_runs WHERE recency = 1").fetchone()
        rows = conn.execute(q.CHANGES_LATEST).fetchall()
        return _report(rows, latest["run_id"] if latest and rows else None)
    if conn.execute(q.RUN_EXISTS, {"run": run}).fetchone() is None:
        raise HTTPException(404, f"no run {run!r} with measurements")
    return _report(conn.execute(q.CHANGES_FOR_RUN, {"run": run}).fetchall(), run)


# ------------------------------------------------------------------------------ runs --
@app.get("/runs", response_model=list[RunStatus], dependencies=[Depends(require_key)])
def runs(conn: Conn, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """Every run, newest first, with what it set out to measure and how far it got.
    A run with `not_reached > 0` is incomplete: re-run it before trusting its report."""
    return conn.execute(q.RUNS, {"limit": limit}).fetchall()
