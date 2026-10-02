"""API keys, checked by the database, not by this process.

Every data endpoint depends on `require_key`. It hashes the `X-API-Key` header and asks
auth.check_key (pg/schema/007), which decides, counts the key's last minute against its
limit, and logs the decision, all in one statement. The API's login still cannot write any
table; the function is the only path to the log, and it only appends.

    401  no key, unknown key, revoked key
    429  over the key's per-minute limit (Retry-After: 60)
"""
from __future__ import annotations

import hashlib
from typing import Annotated

import psycopg
from fastapi import Depends, Header, HTTPException, Request

from .db import get_conn

HEADER = "X-API-Key"


def check(conn: psycopg.Connection, key: str | None, surface: str, path: str) -> tuple[int | None, str]:
    h = hashlib.sha256(key.encode()).hexdigest() if key else None
    with conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")      # the login defaults to read-only; the log write is the function's
        row = conn.execute("SELECT key_id, decision FROM auth.check_key(%s, %s, %s)", (h, surface, path)).fetchone()
    return (row["key_id"], row["decision"]) if isinstance(row, dict) else (row[0], row[1])


def require_key(request: Request, conn: Annotated[psycopg.Connection, Depends(get_conn)],
                x_api_key: Annotated[str | None, Header(alias=HEADER, description="your API key")] = None) -> int:
    key_id, decision = check(conn, x_api_key, "api", request.url.path)
    if decision == "ok":
        return key_id
    if decision == "rate_limited":
        raise HTTPException(429, "rate limit for this key exceeded", headers={"Retry-After": "60"})
    raise HTTPException(401, {"missing": f"send your key in the {HEADER} header",
                              "unknown": "unknown API key", "revoked": "this API key was revoked"}[decision])
