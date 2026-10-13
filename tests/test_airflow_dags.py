"""The Airflow layer, checked without Airflow installed (CI does not install it).

What must never break:
  - the guard: these DAGs connect only to the copy database xmetrics_af, never to xmetrics;
  - the Athena check is the dashboard check: the same nine figures, in the same order, as
    dashboards/checks.sql (the file the Postgres side of the comparison runs);
  - the DAG's shape: the tasks, the retries, one run at a time.
The DAG file itself is read as a syntax tree, so nothing here imports airflow.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAGS = ROOT / "airflow" / "dags"
sys.path.insert(0, str(DAGS))

import _lake  # noqa: E402
import _xm  # noqa: E402


# The guard parses the DSN with psycopg. tests.yml installs it (and fails on any skip);
# weekly.yml installs only [dev], so there these three skip, like the Postgres tests do.
needs_psycopg = pytest.mark.skipif(__import__("importlib").util.find_spec("psycopg") is None,
                                   reason="psycopg not installed (weekly.yml installs only [dev])")


@needs_psycopg
def test_guard_refuses_the_production_database(monkeypatch):
    monkeypatch.setenv("XMETRICS_AF_DSN", "postgresql://xmetrics:x@host.docker.internal:5432/xmetrics")
    with pytest.raises(RuntimeError, match="xmetrics_af"):
        _xm.dsn()


@needs_psycopg
def test_guard_accepts_only_the_copy(monkeypatch):
    dsn = "postgresql://xmetrics:x@host.docker.internal:5432/xmetrics_af"
    monkeypatch.setenv("XMETRICS_AF_DSN", dsn)
    assert _xm.dsn() == dsn


@needs_psycopg
def test_guard_needs_a_dsn(monkeypatch):
    monkeypatch.delenv("XMETRICS_AF_DSN", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        _xm.dsn()


def _aliases(sql: str) -> list[str]:
    body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    # a column alias ends its select item (",", ";" or the end); "CAST(x AS integer)" does not
    return re.findall(r"\)\s+AS\s+(\w+)\s*(?=[,;]|$)", body, flags=re.M)


def test_athena_checks_are_the_dashboard_figures():
    dashboard = _aliases((ROOT / "dashboards" / "checks.sql").read_text(encoding="utf-8"))
    assert len(dashboard) == 9
    assert _lake.NAMES == dashboard
    assert _aliases(_lake.checks_sql("2026-10-13")) == dashboard


def test_checks_read_one_snapshot_only():
    sql = _lake.checks_sql("2026-10-13")
    assert sql.count("dt = '2026-10-13'") == 9


def test_ddl_and_partition_point_at_the_lake():
    ddl = _lake.ddl("latest", [("handle", "string"), ("flags", "array<string>")])
    assert ddl.startswith("CREATE EXTERNAL TABLE IF NOT EXISTS xmetrics_lake.latest")
    assert "PARTITIONED BY (dt string)" in ddl and "STORED AS PARQUET" in ddl
    assert f"LOCATION 's3://{_lake.BUCKET}/xmetrics/latest/'" in ddl
    part = _lake.add_partition("latest", "2026-10-13")
    assert "ADD IF NOT EXISTS PARTITION (dt = '2026-10-13')" in part
    assert part.endswith(f"'s3://{_lake.BUCKET}/xmetrics/latest/dt=2026-10-13/'")


def test_both_sides_compare_as_the_same_text():
    from datetime import datetime, timezone
    from decimal import Decimal
    pg = [94, Decimal("0.1555"), 2500000, 19, 7, 0, "r1", datetime(2026, 10, 8, 2, 37, 8, 123, tzinfo=timezone.utc), 4]
    athena = ["94", "0.1555", "2500000", "19", "7", "0", "r1", "2026-10-08 02:37:08.000", "4"]
    assert [_lake.normalise(n, v) for n, v in zip(_lake.NAMES, pg)] == \
           [_lake.normalise(n, v) for n, v in zip(_lake.NAMES, athena)]


def _dag_tree():
    return ast.parse((DAGS / "xmetrics_postprocess.py").read_text(encoding="utf-8"))


def test_dag_tasks_and_settings():
    tree = _dag_tree()
    tasks = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
             and any(getattr(d, "id", None) == "task" for d in f.decorator_list)}
    assert tasks == {"snapshot", "load", "diff", "checks", "export_s3", "athena_tables", "athena_checks"}
    dag = next(c for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "dag")
    kw = {k.arg: k.value for k in dag.keywords}
    assert ast.literal_eval(kw["dag_id"]) == "xmetrics_postprocess"
    assert ast.literal_eval(kw["max_active_runs"]) == 1
    assert ast.literal_eval(kw["catchup"]) is False
    retries = next(v for k, v in zip(kw["default_args"].keys, kw["default_args"].values)
                   if ast.literal_eval(k) == "retries")
    assert ast.literal_eval(retries) == 3


def test_dag_steps_call_the_pipeline_code():
    src = (DAGS / "xmetrics_postprocess.py").read_text(encoding="utf-8")
    assert "from ops.pipeline import step_load" in src
    assert "from ops.pipeline import step_diff" in src
    assert "mart.v_validation_issues" in src
