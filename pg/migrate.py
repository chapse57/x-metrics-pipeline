"""Apply pg/schema/*.sql in order. Every file is idempotent, so `migrate` can run on every
deploy and on every test session; a `pg.schema_migrations` table records what ran and when.

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
    """Run every migration file in order, in one transaction. Returns the filenames applied."""
    applied = []
    with conn.transaction():
        conn.execute(_LEDGER)
        for path in migration_files():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO pg.schema_migrations (filename, applied_at) VALUES (%s, %s) "
                "ON CONFLICT (filename) DO UPDATE SET applied_at = EXCLUDED.applied_at, "
                "runs = pg.schema_migrations.runs + 1",
                (path.name, datetime.now(timezone.utc)),
            )
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", help="PostgreSQL DSN (default: $XMETRICS_PG_DSN)")
    args = p.parse_args(argv)
    with psycopg.connect(args.dsn or dsn_from_env()) as conn:
        for name in migrate(conn):
            print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
