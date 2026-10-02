"""Create or update the login user the API connects as — a member of xmetrics_reader, which
pg/schema/005_reader_role.sql made read-only. Run as the database owner.

    python -m pg.roles reader --user xmetrics_api --password '...'      # DSN from $XMETRICS_PG_DSN
    python -m pg.roles reader                                            # user/password from $XMETRICS_API_USER / $XMETRICS_API_PASSWORD

Idempotent: an existing user gets its password reset and its membership re-asserted. The
resulting DSN for the API is postgresql://<user>:<password>@<host>:<port>/<db>.

API keys (pg/schema/007): printed once, only the SHA-256 is stored.

    python -m pg.roles key --name acme --per-minute 60       # prints the new key
    python -m pg.roles revoke --name acme
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys

import psycopg
from psycopg import sql


def ensure_reader(conn: psycopg.Connection, user: str, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone()
    ident = sql.Identifier(user)
    with conn.transaction():
        if exists:
            conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(ident, sql.Literal(password)))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(ident, sql.Literal(password)))
        conn.execute(sql.SQL("GRANT xmetrics_reader TO {}").format(ident))
        # belt and braces: even if a grant is added by mistake later, the session is read-only
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(conn: psycopg.Connection, name: str, per_minute: int = 60, key: str | None = None) -> str:
    """A new API key for `name` (replacing any earlier one of that name). Returned once, stored hashed."""
    key = key or "xm_" + secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("INSERT INTO auth.api_key (name, key_hash, per_minute) VALUES (%s, %s, %s) "
                     "ON CONFLICT (name) DO UPDATE SET key_hash = EXCLUDED.key_hash, per_minute = EXCLUDED.per_minute, "
                     "revoked_at = NULL", (name, key_hash(key), per_minute))
    return key


def revoke_key(conn: psycopg.Connection, name: str) -> bool:
    with conn.transaction():
        return conn.execute("UPDATE auth.api_key SET revoked_at = now() WHERE name = %s AND revoked_at IS NULL",
                            (name,)).rowcount == 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reader", help="create/update a read-only login user for the API")
    r.add_argument("--dsn", help="owner DSN (default: $XMETRICS_PG_DSN)")
    r.add_argument("--user", default=os.environ.get("XMETRICS_API_USER", "xmetrics_api"))
    r.add_argument("--password", default=os.environ.get("XMETRICS_API_PASSWORD"))
    k = sub.add_parser("key", help="create (or replace) an API key; printed once")
    k.add_argument("--dsn")
    k.add_argument("--name", required=True)
    k.add_argument("--per-minute", type=int, default=60)
    k.add_argument("--key", help="use this key instead of a random one (e.g. a fixed local dev key)")
    v = sub.add_parser("revoke", help="revoke an API key by name")
    v.add_argument("--dsn")
    v.add_argument("--name", required=True)
    args = p.parse_args(argv)
    dsn = args.dsn or os.environ.get("XMETRICS_PG_DSN")
    if not dsn:
        sys.exit("set XMETRICS_PG_DSN or pass --dsn")
    if args.cmd == "key":
        with psycopg.connect(dsn) as conn:
            print(create_key(conn, args.name, args.per_minute, args.key))
        return 0
    if args.cmd == "revoke":
        with psycopg.connect(dsn) as conn:
            print("revoked" if revoke_key(conn, args.name) else "no active key with that name")
        return 0
    if not args.password:
        sys.exit("pass --password or set XMETRICS_API_PASSWORD")
    with psycopg.connect(dsn) as conn:
        ensure_reader(conn, args.user, args.password)
    print(f"reader login {args.user!r} is a member of xmetrics_reader")
    return 0


if __name__ == "__main__":
    sys.exit(main())
