"""What the MCP server can do, as plain functions, so they are tested without an MCP client.

Same guarantees as the HTTP API, because they are the same pieces:
  - the connection is the read-only login (XMETRICS_MCP_DSN, else XMETRICS_API_DSN);
  - every call is checked and logged by auth.check_key with the key in XMETRICS_MCP_KEY
    (surface 'mcp', path 'mcp:<tool>'), so the per-key limit covers both surfaces;
  - SQL is the API's own (api/queries.py), nothing built from input;
  - no result is longer than MAX_ROWS rows. A cut result says so: `truncated`, `cap`, `total`.
    An assistant that reads "50 rows" should know there were 95.
"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from . import queries as q
from .auth import check

MAX_ROWS = int(os.environ.get("XMETRICS_MCP_MAX_ROWS", "50"))


class Denied(PermissionError):
    pass


def _connect() -> psycopg.Connection:
    dsn = os.environ.get("XMETRICS_MCP_DSN") or os.environ.get("XMETRICS_API_DSN")
    if not dsn:
        raise RuntimeError("set XMETRICS_MCP_DSN (a read-only login, like the API's)")
    return psycopg.connect(dsn, autocommit=True, connect_timeout=5, options="-c timezone=UTC", row_factory=dict_row)


def _authorize(conn, tool: str) -> None:
    _, decision = check(conn, os.environ.get("XMETRICS_MCP_KEY"), "mcp", f"mcp:{tool}")
    if decision != "ok":
        raise Denied(f"{tool}: {decision} API key (set XMETRICS_MCP_KEY)")


def _capped(rows: list[dict], total: int | None = None) -> dict:
    total = len(rows) if total is None else total
    return {"rows": rows[:MAX_ROWS], "returned": min(len(rows), MAX_ROWS), "total": total,
            "truncated": total > MAX_ROWS, "cap": MAX_ROWS}


def search_accounts(tier: str | None = None, min_engagement: float | None = None,
                    sort: str = q.DEFAULT_SORT, limit: int = 25) -> dict:
    if sort not in q.SORTS:
        raise ValueError(f"sort must be one of {sorted(q.SORTS)}")
    if tier is not None and tier not in q.TIERS:
        raise ValueError(f"tier must be one of {q.TIERS}")
    with _connect() as conn:
        _authorize(conn, "search_accounts")
        rows = conn.execute(q.ACCOUNTS.format(order_by=q.SORTS[sort]),
                            {"tier": tier, "min_engagement": min_engagement, "status": None,
                             "limit": min(max(limit, 1), MAX_ROWS + 1), "offset": 0}).fetchall()
    total = rows[0]["total"] if rows else 0
    out = _capped([{k: v for k, v in r.items() if k != "total"} for r in rows], total)
    if limit < out["returned"]:
        out["rows"], out["returned"] = out["rows"][:limit], limit
    return out


def account(handle: str) -> dict:
    h = handle.strip().lstrip("@").lower()
    with _connect() as conn:
        _authorize(conn, "account")
        row = conn.execute(q.ACCOUNT, {"handle": h}).fetchone()
        if row is None:
            return {"error": f"no account @{h}"}
        history = conn.execute(q.HISTORY, {"handle": h}).fetchall()
    return {"account": row, "history": _capped(history)}


def changes() -> dict:
    with _connect() as conn:
        _authorize(conn, "changes")
        rows = conn.execute(q.CHANGES_LATEST).fetchall()
    flagged = [r for r in rows if r["flags"]]                  # what moved first, so a cap keeps the signal
    rest = [r for r in rows if not r["flags"]]
    return _capped(flagged + rest)
