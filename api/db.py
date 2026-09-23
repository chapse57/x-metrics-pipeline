"""One connection per request, as the read-only user.

The DSN comes from XMETRICS_API_DSN and must point at a login that is a member of
xmetrics_reader (pg/schema/005, `python -m pg.roles reader`). The API has no other DSN and
no write path: if the role could write, that would be a bug in the role, not something this
module could exploit.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

DSN_ENV = "XMETRICS_API_DSN"


def dsn() -> str:
    value = os.environ.get(DSN_ENV)
    if not value:
        raise RuntimeError(f"{DSN_ENV} is not set (a read-only login: see pg/README.md)")
    return value


def connect(**kw) -> psycopg.Connection:
    """A read-only connection with timestamps reported in UTC, whatever the server's zone —
    an API should not change its answers with the time zone of the box it runs on."""
    conn = psycopg.connect(dsn(), autocommit=True, connect_timeout=5, options="-c timezone=UTC", **kw)
    return conn


def get_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency. autocommit: every statement is its own read; nothing to roll back."""
    with connect(row_factory=dict_row) as conn:
        yield conn
