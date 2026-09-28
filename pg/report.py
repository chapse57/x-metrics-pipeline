"""The change report, read back from SQL as the same DiffResult xmetrics.diff builds in Python.

mart.changes_since(run) returns xmetrics.diff.Change, column for column (tests/test_pg.py
holds the two equal). This module turns those rows back into a DiffResult, so changes.md and
changes.json can be written from the database — the report the dashboard and the API show —
with the writers Python already has.

Why write the file from SQL and not from Python: the SQLite file on the collecting machine
holds the runs that machine made; the database holds every run from every file. A baseline
that lives only in another file (the hand-measured import) is visible to SQL and not to the
local Python diff. The database is the whole history, so the weekly report comes from it.
"""
from __future__ import annotations

from dataclasses import fields

import psycopg

from xmetrics import diff as df

COLUMNS = [f.name for f in fields(df.Change)]

_CHANGES = "SELECT * FROM mart.changes_since(%s)"
_STATUS = "SELECT targets, measured, missing, failed, not_reached FROM mart.v_run_status WHERE run_id = %s"
_TARGETS = "SELECT handle, outcome FROM raw.run_targets WHERE run_id = %s ORDER BY handle"


def changes_from_sql(conn: psycopg.Connection, run_id: str) -> df.DiffResult:
    rows = conn.execute(_CHANGES, (run_id,)).fetchall()
    changes = [df.Change(**dict(zip(COLUMNS, r))) for r in rows]
    for c in changes:
        c.flags = list(c.flags or [])
    status = conn.execute(_STATUS, (run_id,)).fetchone()
    if status is None:
        raise ValueError(f"run {run_id!r} is not in the database")
    # same shape as xmetrics.diff.compare(): every outcome, zero included, in Store.OUTCOMES order
    targets = dict(zip(("pending", "measured", "missing", "error"), (status[4], status[1], status[2], status[3])))
    outcomes = conn.execute(_TARGETS, (run_id,)).fetchall()
    used = sorted({c.run_prev for c in changes if c.run_prev})
    return df.DiffResult(
        run_prev=used[0] if len(used) == 1 else None, run_now=run_id, changes=changes,
        prev_runs=used,
        targets=targets,
        failed=[h for h, o in outcomes if o == "error"],
        not_reached=[h for h, o in outcomes if o == "pending"],
        mode="baseline",
    )
