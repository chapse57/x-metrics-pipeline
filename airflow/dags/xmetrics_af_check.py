"""Smoke check for the local Airflow, run by hand from the UI: is the setup right before any real DAG?

Three read-only tasks:
  guard        XMETRICS_AF_DSN names xmetrics_af and nothing else
  code         the repo's pg/, xmetrics/, ops/ import inside the container (PYTHONPATH + psycopg 3)
  counts       row counts in the copy, to set beside the `ops.backup drill` output

No schedule, no writes. The real post-processing DAG (load -> diff -> checks) comes after the
2026-10-14 unattended-run check.
"""
from __future__ import annotations

from datetime import datetime

from airflow.sdk import dag, task

TABLES = ("raw.runs", "raw.accounts", "raw.measurements", "raw.posts", "raw.run_targets", "ops.pipeline_runs")


@dag(dag_id="xmetrics_af_check", schedule=None, start_date=datetime(2026, 10, 1), catchup=False,
     tags=["x-metrics", "copy-db", "read-only"])
def xmetrics_af_check():

    @task
    def guard() -> str:
        import psycopg.conninfo as ci
        import _xm
        return ci.conninfo_to_dict(_xm.dsn())["dbname"]      # the name only; the DSN has a password

    @task
    def code() -> list[str]:
        import ops.notify
        import pg.load
        import pg.report
        import xmetrics.diff
        return [m.__name__ for m in (pg.load, pg.report, xmetrics.diff, ops.notify)]

    @task
    def counts() -> dict[str, int]:
        import _xm
        with _xm.connect() as conn:
            conn.read_only = True
            out = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
            out["_database"] = conn.execute("SELECT current_database()").fetchone()[0]
        print(out)
        return out

    guard() >> code() >> counts()


xmetrics_af_check()
