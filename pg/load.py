"""SQLite -> PostgreSQL. Copies runs, accounts, measurements and posts from one or more
xmetrics SQLite files into the `raw` schema, column for column.

Idempotent by construction: every insert is ON CONFLICT ... DO UPDATE on the same primary
key the SQLite tables use, so loading the same file twice changes nothing, and loading a
newer file over an older one updates in place. tests/test_pg.py asserts both.

    python -m pg.load out/x_legacy_claude.db out/x.db        # DSN from $XMETRICS_PG_DSN
    python -m pg.load --run 20260918T020745Z-playwright out/x.db   # one run only (re-processing)

Accounts are shared across files (the same handle appears in the legacy import and in live
runs). The row with the newer `updated_at` wins, whichever file is loaded first.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import psycopg


@dataclass
class LoadResult:
    source_db: str
    runs: int
    accounts: int
    measurements: int
    posts: int

    def __str__(self) -> str:
        return (f"{self.source_db}: runs {self.runs}, accounts {self.accounts}, "
                f"measurements {self.measurements}, posts {self.posts}")


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _rows(sq: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    sq.row_factory = sqlite3.Row
    return sq.execute(sql, params).fetchall()


_UPSERT_RUN = """
INSERT INTO raw.runs (run_id, started_at, finished_at, source, note, source_db)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO UPDATE SET
  started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
  source = EXCLUDED.source, note = EXCLUDED.note, source_db = EXCLUDED.source_db,
  loaded_at = now()
"""

_UPSERT_ACCOUNT = """
INSERT INTO raw.accounts (handle, display, bio, bio_url, dm_open, niche, niche_source, fit_note,
  hook_link, hook_note, status, screen_reason, created_at, updated_at, source_db)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (handle) DO UPDATE SET
  display = EXCLUDED.display, bio = EXCLUDED.bio, bio_url = EXCLUDED.bio_url,
  dm_open = EXCLUDED.dm_open, niche = EXCLUDED.niche, niche_source = EXCLUDED.niche_source,
  fit_note = EXCLUDED.fit_note, hook_link = EXCLUDED.hook_link, hook_note = EXCLUDED.hook_note,
  status = EXCLUDED.status, screen_reason = EXCLUDED.screen_reason,
  created_at = LEAST(raw.accounts.created_at, EXCLUDED.created_at),
  updated_at = EXCLUDED.updated_at, source_db = EXCLUDED.source_db, loaded_at = now()
WHERE EXCLUDED.updated_at >= raw.accounts.updated_at
"""

_UPSERT_MEASUREMENT = """
INSERT INTO raw.measurements (handle, run_id, measured_at, followers, posts_measured,
  range_start, range_end, median_likes, median_replies, median_reposts, median_views,
  engagement_rate, views_to_followers, tier, days_since_last_post)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (handle, run_id) DO UPDATE SET
  measured_at = EXCLUDED.measured_at, followers = EXCLUDED.followers,
  posts_measured = EXCLUDED.posts_measured, range_start = EXCLUDED.range_start,
  range_end = EXCLUDED.range_end, median_likes = EXCLUDED.median_likes,
  median_replies = EXCLUDED.median_replies, median_reposts = EXCLUDED.median_reposts,
  median_views = EXCLUDED.median_views, engagement_rate = EXCLUDED.engagement_rate,
  views_to_followers = EXCLUDED.views_to_followers, tier = EXCLUDED.tier,
  days_since_last_post = EXCLUDED.days_since_last_post, loaded_at = now()
"""

_UPSERT_POST = """
INSERT INTO raw.posts (handle, run_id, status_id, posted_at, replies, reposts, likes, bookmarks,
  views, is_repost, is_pinned, is_reply, raw_label)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (handle, run_id, status_id) DO UPDATE SET
  posted_at = EXCLUDED.posted_at, replies = EXCLUDED.replies, reposts = EXCLUDED.reposts,
  likes = EXCLUDED.likes, bookmarks = EXCLUDED.bookmarks, views = EXCLUDED.views,
  is_repost = EXCLUDED.is_repost, is_pinned = EXCLUDED.is_pinned, is_reply = EXCLUDED.is_reply,
  raw_label = EXCLUDED.raw_label, loaded_at = now()
"""


def load(conn: psycopg.Connection, sqlite_path: str | Path, run_id: str | None = None) -> LoadResult:
    """Load one SQLite file. With run_id, only that run's rows (plus every account, which is
    cheap and keeps foreign keys satisfied). One transaction: a failure loads nothing."""
    path = Path(sqlite_path)
    if not path.exists():
        raise FileNotFoundError(path)
    sq = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src = path.name
    run_filter = "WHERE run_id = ?" if run_id else ""
    run_params = (run_id,) if run_id else ()

    runs = _rows(sq, f"SELECT * FROM runs {run_filter}", run_params)
    if run_id and not runs:
        raise ValueError(f"run {run_id!r} not in {src}")
    accounts = _rows(sq, "SELECT * FROM accounts")
    measurements = _rows(sq, f"SELECT * FROM measurements {run_filter}", run_params)
    posts = _rows(sq, f"SELECT * FROM posts {run_filter}", run_params)

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(_UPSERT_RUN, [
            (r["run_id"], _ts(r["started_at"]), _ts(r["finished_at"]), r["source"], r["note"], src)
            for r in runs])
        cur.executemany(_UPSERT_ACCOUNT, [
            (a["handle"], a["display"], a["bio"], a["bio_url"], a["dm_open"], a["niche"], a["niche_source"],
             a["fit_note"], a["hook_link"], a["hook_note"], a["status"], a["screen_reason"],
             _ts(a["created_at"]), _ts(a["updated_at"]), src)
            for a in accounts])
        cur.executemany(_UPSERT_MEASUREMENT, [
            (m["handle"], m["run_id"], _ts(m["measured_at"]), m["followers"], m["posts_measured"],
             _date(m["range_start"]), _date(m["range_end"]),
             m["median_likes"], m["median_replies"], m["median_reposts"], m["median_views"],
             m["engagement_rate"], m["views_to_followers"], m["tier"], m["days_since_last_post"])
            for m in measurements])
        cur.executemany(_UPSERT_POST, [
            (p["handle"], p["run_id"], p["status_id"], _ts(p["posted_at"]), p["replies"], p["reposts"],
             p["likes"], p["bookmarks"], p["views"], p["is_repost"], p["is_pinned"], p["is_reply"], p["raw_label"])
            for p in posts])
    sq.close()
    return LoadResult(src, len(runs), len(accounts), len(measurements), len(posts))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("sqlite", nargs="+", help="xmetrics SQLite file(s), loaded in the order given")
    p.add_argument("--dsn", help="PostgreSQL DSN (default: $XMETRICS_PG_DSN)")
    p.add_argument("--run", help="load only this run_id (re-processing a single run)")
    args = p.parse_args(argv)
    dsn = args.dsn or os.environ.get("XMETRICS_PG_DSN")
    if not dsn:
        sys.exit("set XMETRICS_PG_DSN or pass --dsn")
    with psycopg.connect(dsn) as conn:
        for f in args.sqlite:
            print(load(conn, f, run_id=args.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
