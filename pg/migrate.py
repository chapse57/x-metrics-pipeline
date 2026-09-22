"""Apply pg/schema/*.sql in order, each file once. A `pg.schema_migrations` table records what
ran and when; files already in it are skipped, so `migrate` is safe to run on every deploy and
every test session, and a later file may drop and rebuild what an earlier one created (001
made mart.v_changes with one column set; 002 rebuilds it with another). To change the schema,
add a file — never edit one that has been applied somewhere.

    python -m pg.migrate                      # DSN from $XMETRICS_PG_DSN
    python -m pg.migrate --dsn postgresql://x_admin@localhost:5432/xmetrics
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

SCHEMA_DIR = Path(__file__).parent / "schema"

_LEDGER = """
CREATE SCHEMA IF NOT EXISTS pg;
CREATE TABLE IF NOT EXISTS pg.schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL,
  runs       int NOT NULL DEFAULT 1
);
"""


def dsn_from_env() -> str:
    dsn = os.environ.get("XMETRICS_PG_DSN")
    if not dsn:
        sys.exit("set XMETRICS_PG_DSN (e.g. postgresql://x_admin:pw@localhost:5432/xmetrics) or pass --dsn")
    return dsn


def migration_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def migrate(conn: psycopg.Connection) -> list[str]:
    """Run every migration file not yet in the ledger, in order, in one transaction.
    Returns the filenames applied (empty when the database is already current)."""
    applied = []
    with conn.transaction():
        conn.execute(_LEDGER)
        done = {r[0] for r in conn.execute("SELECT filename FROM pg.schema_migrations")}
        for path in migration_files():
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO pg.schema_migrations (filename, applied_at) VALUES (%s, %s)",
                         (path.name, datetime.now(timezone.utc)))
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", help="PostgreSQL DSN (default: $XMETRICS_PG_DSN)")
    args = p.parse_args(argv)
    with psycopg.connect(args.dsn or dsn_from_env()) as conn:
        applied = migrate(conn)
        for name in applied:
            print(f"applied {name}")
        if not applied:
            print("up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
