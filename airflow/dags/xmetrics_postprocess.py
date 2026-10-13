"""x-metrics post-processing on the copy database: load -> diff -> checks, one week per run.

What it is: the steps after collection, orchestrated by Airflow, against `xmetrics_af` (a
restored copy). Collection stays with the Windows scheduled task, because it needs the
logged-in browser on the desktop; this DAG never collects and never touches `xmetrics`.

Each task calls the code the scheduled pipeline already uses (ops.pipeline.step_load /
step_diff, mart.v_validation_issues). Nothing is re-implemented here.

One run = one week. The run for logical date L handles the newest collector run that
started in [L - 7 days, L) and measured at least one account. A week with no collection is
skipped, not failed: that is what happened in the week of 2026-09-28 (the 10-04 run died
before it wrote anything) and in the week of 2026-09-07.

    backfill:  airflow backfill create --dag-id xmetrics_postprocess --from-date 2026-09-01 --to-date 2026-10-12
    one run:   trigger from the UI; params.run_id pins a collector run, params.fail_at rehearses a failure

Retries: 3, 30 seconds apart (short so a rehearsal finishes in two minutes). Slack gets one
message, after the last retry fails, through the same ops.notify the pipeline uses.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from airflow.sdk.exceptions import AirflowSkipException
from airflow.sdk import Param, dag, get_current_context, task

SRC_DB = Path(os.environ.get("XMETRICS_SRC_OUT", "/opt/xmetrics-out")) / "x.db"
WORK = Path(os.environ.get("XMETRICS_AF_WORK", "/opt/airflow/work"))
COUNTED = ("raw.runs", "raw.accounts", "raw.measurements", "raw.posts", "raw.run_targets")


def _rehearse(step: str) -> None:
    """params.fail_at == step: fail on purpose, on every try, so retries and the alert can be seen."""
    if get_current_context()["params"].get("fail_at") == step:
        raise RuntimeError(f"rehearsal: fail_at={step}")


def _ctx(info: dict):
    from ops.pipeline import Context
    import _xm
    return Context(db=Path(info["db"]), out=Path(info["out"]), dsn=_xm.dsn(), run_id=info["run_id"])


def _counts() -> dict[str, int]:
    import _xm
    with _xm.connect() as conn:
        conn.read_only = True
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNTED}


def alert_on_failure(context) -> None:
    """Called once a task has used all its tries. Same notifier as ops/pipeline.py."""
    from ops import notify
    ti = context["ti"]
    err = str(context.get("exception") or "(no error text)").strip().splitlines()[0]
    msg = (f":red_circle: Airflow `{ti.dag_id}` failed at *{ti.task_id}* after {ti.try_number} tries "
           f"(copy database xmetrics_af, logical date {context.get('logical_date')})\n> {err}")
    notify.from_env().send(msg)


@dag(
    dag_id="xmetrics_postprocess",
    schedule="0 0 * * 1",                     # Mondays 00:00 UTC: each run closes the week before it
    start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    catchup=False,                            # past weeks only by an explicit backfill
    max_active_runs=1,                        # one load into the copy at a time
    default_args={"retries": 3, "retry_delay": timedelta(seconds=30), "on_failure_callback": alert_on_failure},
    params={
        # both optional: leave empty for a normal run (null, so the trigger form does not demand them)
        "run_id": Param(None, type=["null", "string"],
                        description="optional: collector run_id to process instead of the week's newest"),
        "fail_at": Param(None, type=["null", "string"], enum=[None, "snapshot", "load", "diff", "checks", "export_s3", "athena_tables", "athena_checks"],
                         description="optional rehearsal: fail inside this task"),
    },
    tags=["x-metrics", "copy-db"],
)
def xmetrics_postprocess():

    @task
    def snapshot() -> dict:
        """Copy the collector's SQLite file and pick the run. A copy, because xmetrics.store
        writes to the file it opens, and the original belongs to the Windows pipeline."""
        _rehearse("snapshot")
        context = get_current_context()
        # a manual run may come without a logical date: then "the week up to now"
        end = (context.get("logical_date") or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = end - timedelta(days=7)
        work = WORK / context["run_id"].replace(":", "-").replace("+", "_")
        work.mkdir(parents=True, exist_ok=True)
        db = work / "x.db"
        shutil.copy2(SRC_DB, db)

        pinned = context["params"].get("run_id") or ""
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as sq:
            if pinned:
                row = sq.execute("SELECT run_id FROM runs WHERE run_id = ?", (pinned,)).fetchone()
                if not row:
                    raise ValueError(f"run {pinned!r} is not in {SRC_DB.name}")
            else:
                row = sq.execute(
                    """SELECT r.run_id FROM runs r
                       WHERE r.started_at >= ? AND r.started_at < ?
                         AND EXISTS (SELECT 1 FROM measurements m WHERE m.run_id = r.run_id)
                       ORDER BY r.started_at DESC LIMIT 1""",
                    (start.isoformat(), end.isoformat())).fetchone()
        if not row:
            raise AirflowSkipException(f"no collection with measurements in [{start:%Y-%m-%d}, {end:%Y-%m-%d})")
        print(f"week [{start:%Y-%m-%d}, {end:%Y-%m-%d}) -> run {row[0]}")
        return {"run_id": row[0], "db": str(db), "out": str(work / "out")}

    @task
    def load(info: dict) -> dict:
        """pg.load through ops.pipeline.step_load: that run only, upserted. Row counts before
        and after are kept: loading a run that is already there must change nothing."""
        _rehearse("load")
        from ops.pipeline import step_load
        before = _counts()
        detail = step_load(_ctx(info))
        after = _counts()
        print(detail)
        print({"before": before, "after": after})
        return {**info, "rows_before": before, "rows_after": after}

    @task
    def diff(info: dict) -> str:
        """The change report from SQL, checked against Python on the same file: ops.pipeline.step_diff."""
        _rehearse("diff")
        from ops.pipeline import step_diff
        detail = step_diff(_ctx(info))
        print(detail)
        return detail

    @task
    def checks(info: dict) -> dict:
        """The validation rules as SQL (mart.v_validation_issues). Any 'error' fails the run."""
        _rehearse("checks")
        import _xm
        with _xm.connect() as conn:
            conn.read_only = True
            by = dict(conn.execute(
                "SELECT severity, count(*) FROM mart.v_validation_issues GROUP BY 1").fetchall())
            measured = conn.execute(
                "SELECT measured FROM mart.v_run_status WHERE run_id = %s", (info["run_id"],)).fetchone()
        print({"run": info["run_id"], "measured": measured and measured[0], "issues": by})
        if not measured or not measured[0]:
            raise RuntimeError(f"run {info['run_id']} has no measured accounts in the copy database")
        if by.get("error"):
            raise RuntimeError(f"{by['error']} validation error(s) in mart.v_validation_issues")
        return {"run": info["run_id"], "measured": measured[0], "issues": by}

    @task
    def export_s3(info: dict) -> dict:
        """mart.v_latest / v_changes / v_runs -> Parquet -> s3://<bucket>/xmetrics/<table>/dt=<today>/.
        Skipped without AWS keys, and in a backfill: the views are the database's current state,
        so a past week cannot be snapshotted "as of then" from them."""
        _rehearse("export_s3")
        import _lake
        import _xm
        context = get_current_context()
        if not _lake.configured():
            raise AirflowSkipException("AWS keys are not set in airflow/.env")
        if "backfill" in str(context["dag_run"].run_type).lower():
            raise AirflowSkipException("backfill: mart views are current state, not as of that week")
        import boto3
        dt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s3 = boto3.client("s3")
        tables = {}
        with _xm.connect() as conn:
            conn.read_only = True
            for name, view in _lake.VIEWS.items():
                table, columns = _lake.snapshot(conn, view)
                tables[name] = {**_lake.put_parquet(s3, table, name, dt), "columns": columns}
        print({n: {k: v for k, v in t.items() if k != "columns"} for n, t in tables.items()})
        return {"dt": dt, "tables": tables}

    @task
    def athena_tables(exp: dict) -> dict:
        """Database, external tables (DDL, no crawler) and today's partition."""
        _rehearse("athena_tables")
        import boto3
        import _lake
        client = boto3.client("athena")
        scanned = 0
        scanned += _lake.athena(client, f"CREATE DATABASE IF NOT EXISTS {_lake.DATABASE}", database=None)["scanned_bytes"]
        for name, t in exp["tables"].items():
            scanned += _lake.athena(client, _lake.ddl(name, [tuple(c) for c in t["columns"]]))["scanned_bytes"]
            scanned += _lake.athena(client, _lake.add_partition(name, exp["dt"]))["scanned_bytes"]
        print({"dt": exp["dt"], "tables": list(exp["tables"]), "ddl_scanned_bytes": scanned})
        return exp

    @task
    def athena_checks(exp: dict) -> dict:
        """dashboards/checks.sql in Postgres (the copy) and the same nine figures in Athena over
        today's snapshot. Any difference fails the run."""
        _rehearse("athena_checks")
        import boto3
        import _lake
        import _xm
        sql = Path("/opt/xmetrics/dashboards/checks.sql").read_text(encoding="utf-8")
        with _xm.connect() as conn:
            conn.read_only = True
            pg = conn.execute(sql).fetchone()
        res = _lake.athena(boto3.client("athena"), _lake.checks_sql(exp["dt"]))
        ath = res["rows"][1]                  # row 0 is the header
        table = []
        for i, name in enumerate(_lake.NAMES):
            a, b = _lake.normalise(name, pg[i]), _lake.normalise(name, ath[i])
            table.append({"figure": name, "postgres": a, "athena": b, "same": a == b})
        agree = sum(r["same"] for r in table)
        for r in table:
            print(f"{'OK ' if r['same'] else 'DIFF'} {r['figure']:<20} postgres={r['postgres']}  athena={r['athena']}")
        print(f"{agree} of {len(table)} agree · Athena scanned {res['scanned_bytes']} bytes in {res['ms']} ms")
        if agree != len(table):
            raise RuntimeError(f"Postgres and Athena disagree on {len(table) - agree} of {len(table)} figures")
        return {"agree": agree, "of": len(table), "scanned_bytes": res["scanned_bytes"], "ms": res["ms"]}

    picked = snapshot()
    loaded = load(picked)
    checked = checks(loaded)
    diff(loaded) >> checked
    exported = export_s3(loaded)
    checked >> exported
    athena_checks(athena_tables(exported))


xmetrics_postprocess()
