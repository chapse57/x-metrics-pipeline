"""Airflow starts the Glue Spark job over the podcast lake, waits for it, and checks its numbers.

    start_glue  ->  wait_glue  ->  compare_postgres

The code is media-discovery's discovery/glue.py (mounted read-only at /opt/media), split into its
three steps; nothing is re-implemented here. The job reads s3://<bucket>/media/podcasts/ (Parquet,
4.7 M rows), writes media/aggregates/, and its figures are compared with the same figures computed
in Postgres (database `discovery`, read only). Any difference fails the run.

Retries differ per task on purpose:
- start_glue: 0. A retry would start, and pay for, a second Spark run. (Glue also refuses a second
  concurrent run of the job: MaxConcurrentRuns is 1.)
- wait_glue: 2. It only reads the run's state, so repeating it is harmless.
- compare_postgres: 1. Read only.
Slack gets one message when a task has used its tries (same webhook as the pipeline).

No schedule: the lake is a one-off load of the 2026-09-27 public dump, so there is nothing new to
aggregate on a timer. Trigger from the UI. One run costs about USD 0.035 (measured 2026-10-13).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from airflow.sdk import Param, dag, get_current_context, task

DSN_ENV = "MEDIA_DISCOVERY_DSN"


def _rehearse(step: str) -> None:
    if get_current_context()["params"].get("fail_at") == step:
        raise RuntimeError(f"rehearsal: fail_at={step}")


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} is not set")
    return dsn


def alert_on_failure(context) -> None:
    from ops import notify
    ti = context["ti"]
    err = str(context.get("exception") or "(no error text)").strip().splitlines()[0]
    notify.from_env().send(f":red_circle: Airflow `{ti.dag_id}` failed at *{ti.task_id}* "
                           f"after {ti.try_number} tries\n> {err}")


@dag(
    dag_id="media_lake_glue",
    schedule=None,
    start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0, "retry_delay": timedelta(seconds=30), "on_failure_callback": alert_on_failure},
    params={
        "fail_at": Param(None, type=["null", "string"], enum=[None, "start_glue", "wait_glue", "compare_postgres"],
                         description="optional rehearsal: fail inside this task (start_glue fails before any AWS call)"),
    },
    tags=["media-discovery", "aws", "glue"],
)
def media_lake_glue():

    @task(retries=0)
    def start_glue() -> dict:
        """Upload the Spark script, create or update the Glue job, start one run."""
        _rehearse("start_glue")
        from discovery import glue
        started = glue.start()
        print(started)
        return started

    @task(retries=2, execution_timeout=timedelta(minutes=20))
    def wait_glue(started: dict) -> dict:
        """Poll until the run ends. Anything but SUCCEEDED fails this task."""
        _rehearse("wait_glue")
        from discovery import glue
        finished = glue.wait(started)
        print(finished)
        if finished["state"] != "SUCCEEDED":
            raise RuntimeError(f"Glue run {finished['run_id']} ended {finished['state']}: {finished.get('error')}")
        return finished

    @task(retries=1)
    def compare_postgres(finished: dict) -> dict:
        """The Spark figures against Postgres. Any difference fails the run."""
        _rehearse("compare_postgres")
        from discovery import glue
        result = glue.compare(_dsn(), finished)
        print({k: v for k, v in result.items() if k != "checks"})
        for c in result["checks"]:
            print(("OK   " if c["same"] else "DIFF ") + c["figure"])
        if result["agree"] != result["of"]:
            raise RuntimeError(f"Spark and Postgres differ: {result['agree']} of {result['of']} figures agree")
        return {"run_id": result["run_id"], "agree": result["agree"], "of": result["of"],
                "execution_seconds": result["execution_seconds"], "usd_estimate": result["usd_estimate"]}

    compare_postgres(wait_glue(start_glue()))


media_lake_glue()
