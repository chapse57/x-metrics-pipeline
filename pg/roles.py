"""Create or update the login user the API connects as — a member of xmetrics_reader, which
pg/schema/005_reader_role.sql made read-only. Run as the database owner.

    python -m pg.roles reader --user xmetrics_api --password '...'      # DSN from $XMETRICS_PG_DSN
    python -m pg.roles reader                                            # user/password from $XMETRICS_API_USER / $XMETRICS_API_PASSWORD

Idempotent: an existing user gets its password reset and its membership re-asserted. The
resulting DSN for the API is postgresql://<user>:<password>@<host>:<port>/<db>.
"""
from __future__ import annotations

import argparse
import os
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reader", help="create/update a read-only login user for the API")
    r.add_argument("--dsn", help="owner DSN (default: $XMETRICS_PG_DSN)")
    r.add_argument("--user", default=os.environ.get("XMETRICS_API_USER", "xmetrics_api"))
    r.add_argument("--password", default=os.environ.get("XMETRICS_API_PASSWORD"))
    args = p.parse_args(argv)
    dsn = args.dsn or os.environ.get("XMETRICS_PG_DSN")
    if not dsn:
        sys.exit("set XMETRICS_PG_DSN or pass --dsn")
    if not args.password:
        sys.exit("pass --password or set XMETRICS_API_PASSWORD")
    with psycopg.connect(dsn) as conn:
        ensure_reader(conn, args.user, args.password)
    print(f"reader login {args.user!r} is a member of xmetrics_reader")
    return 0


if __name__ == "__main__":
    sys.exit(main())
