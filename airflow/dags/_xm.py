"""Shared by the x-metrics DAGs: where the copy database is, and the guard that keeps them off production.

Not a DAG (the leading underscore keeps Airflow's DAG processor from treating it as one).
"""
from __future__ import annotations

import os

ALLOWED_DB = "xmetrics_af"     # the copy made by: python -m ops.backup drill --into xmetrics_af --keep
CONNECT_TIMEOUT = 10           # seconds, as in ops/pipeline.py: never wait for the server without a limit


def dsn() -> str:
    """XMETRICS_AF_DSN, but only if it names the copy database. Anything else is refused before
    a connection is opened: the production database `xmetrics` belongs to the Windows scheduled task."""
    import psycopg.conninfo as ci
    value = os.environ.get("XMETRICS_AF_DSN", "")
    if not value:
        raise RuntimeError("XMETRICS_AF_DSN is not set (see airflow/docker-compose.yaml)")
    db = ci.conninfo_to_dict(value).get("dbname")
    if db != ALLOWED_DB:
        raise RuntimeError(f"XMETRICS_AF_DSN points at database {db!r}; these DAGs only touch {ALLOWED_DB!r}")
    return value


def connect(**kw):
    import psycopg
    return psycopg.connect(dsn(), connect_timeout=CONNECT_TIMEOUT, **kw)
