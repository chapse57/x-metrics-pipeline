"""PostgreSQL layer: migration idempotence, load counts, reload invariance, and the one
that matters — mart.changes() in SQL equals xmetrics.diff.compare() in Python, every row,
every column, for every pair of runs in the real database.

Needs a reachable PostgreSQL and XMETRICS_PG_DSN (any database; the raw/core/mart/pg
schemas are dropped and rebuilt at the start of the session). Skipped otherwise, so the
rest of the suite runs without a server.

    XMETRICS_PG_DSN=postgresql://postgres:pw@localhost:5432/xmetrics python -m pytest tests/test_pg.py -q
"""
from __future__ import annotations

import os
import random
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from xmetrics import diff as df
from xmetrics.store import Store

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("XMETRICS_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="XMETRICS_PG_DSN not set")

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = ROOT / "out" / "x.db"
LEGACY_DB = ROOT / "out" / "x_legacy_claude.db"
SQLITE_FILES = [p for p in (LEGACY_DB, LIVE_DB) if p.exists()]

TABLES = ("runs", "accounts", "measurements", "posts")


def pg_counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM raw.{t}").fetchone()[0] for t in TABLES}


def sqlite_counts(path: Path) -> dict[str, int]:
    sq = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: sq.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        sq.close()


def object_inventory(conn) -> set[tuple[str, str, str]]:
    """Every table, view and function in our schemas — to prove a second migrate adds nothing."""
    rows = conn.execute("""
        SELECT table_schema, table_name, table_type FROM information_schema.tables
         WHERE table_schema IN ('raw', 'core', 'mart', 'pg')
        UNION ALL
        SELECT routine_schema, routine_name, routine_type FROM information_schema.routines
         WHERE routine_schema IN ('raw', 'core', 'mart', 'pg')
    """).fetchall()
    return set(rows)


@pytest.fixture(scope="session")
def conn():
    from pg.migrate import migrate
    with psycopg.connect(DSN) as c:
        c.execute("DROP SCHEMA IF EXISTS raw, core, mart, pg CASCADE")
        c.commit()
        migrate(c)
        yield c


# ------------------------------------------------------------------ 1. migration --
def test_migrate_is_idempotent(conn):
    from pg.migrate import migrate
    before = object_inventory(conn)
    migrate(conn)
    after = object_inventory(conn)
    assert after == before
    assert conn.execute("SELECT runs FROM pg.schema_migrations WHERE filename = '001_init.sql'").fetchone()[0] >= 2


# ------------------------------------------------------------------ 2. load counts --
@pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load")
def test_load_counts_match_sqlite(conn):
    from pg.load import load
    for path in SQLITE_FILES:
        res = load(conn, path)
        want = sqlite_counts(path)
        assert (res.runs, res.accounts, res.measurements, res.posts) == tuple(want[t] for t in TABLES)
    # every SQLite run and measurement landed; accounts merge on handle across files
    got = pg_counts(conn)
    per_file = [sqlite_counts(p) for p in SQLITE_FILES]
    assert got["runs"] == sum(c["runs"] for c in per_file)
    assert got["measurements"] == sum(c["measurements"] for c in per_file)
    assert got["posts"] == sum(c["posts"] for c in per_file)
    handles = set()
    for p in SQLITE_FILES:
        sq = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        handles |= {r[0] for r in sq.execute("SELECT handle FROM accounts")}
        sq.close()
    assert got["accounts"] == len(handles)


# --------------------------------------------------------------- 3. reload = no-op --
@pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load")
def test_reload_changes_nothing(conn):
    from pg.load import load
    before = pg_counts(conn)
    snapshot = conn.execute(
        "SELECT handle, run_id, followers, engagement_rate FROM raw.measurements ORDER BY 1, 2").fetchall()
    for path in SQLITE_FILES:
        load(conn, path)
    for path in SQLITE_FILES:          # and once more in the other order
        load(conn, path)
    assert pg_counts(conn) == before
    assert conn.execute(
        "SELECT handle, run_id, followers, engagement_rate FROM raw.measurements ORDER BY 1, 2").fetchall() == snapshot


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no out/x.db")
def test_load_single_run_only_touches_that_run(conn):
    from pg.load import load
    sq = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    run_id = sq.execute("SELECT run_id FROM measurements LIMIT 1").fetchone()[0]
    n_sqlite = sq.execute("SELECT count(*) FROM measurements WHERE run_id = ?", (run_id,)).fetchone()[0]
    sq.close()
    before = pg_counts(conn)
    res = load(conn, LIVE_DB, run_id=run_id)
    assert res.runs == 1 and res.measurements == n_sqlite
    assert pg_counts(conn) == before
    with pytest.raises(ValueError):
        load(conn, LIVE_DB, run_id="no-such-run")


# ------------------------------------------------ 4. SQL changes == Python changes --
COLUMNS = ["handle", "display", "kind", "flags",
           "followers_prev", "followers_now", "followers_delta", "followers_delta_pct",
           "engagement_prev", "engagement_now", "engagement_delta_pp",
           "views_prev", "views_now", "views_delta_pct", "tier_prev", "tier_now",
           "days_since_last_post_prev", "days_since_last_post_now",
           "posts_measured_prev", "posts_measured_now"]


def sql_changes(conn, run_prev: str, run_now: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM mart.changes(%s, %s)", (run_prev, run_now)).fetchall()
    return [dict(zip(COLUMNS, r)) for r in rows]


def python_changes(store: Store, run_prev: str, run_now: str) -> list[dict]:
    return [asdict(c) for c in df.compare(store, run_prev=run_prev, run_now=run_now).changes]


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no out/x.db")
def test_sql_changes_equal_python_changes_for_every_run_pair(conn):
    """The core claim of this layer: the weekly change report computed in SQL is identical to
    the one computed in Python — same rows, same order, same flags, same rounded deltas."""
    from pg.load import load
    load(conn, LIVE_DB)
    store = Store(LIVE_DB)
    runs = df.runs_with_measurements(store)
    assert len(runs) >= 2, "need two runs with measurements in out/x.db"
    pairs = list(zip(runs, runs[1:]))               # every consecutive pair
    pairs.append((runs[0], runs[-1]))               # and first -> last
    checked = 0
    for prev, now in pairs:
        py = python_changes(store, prev, now)
        sql = sql_changes(conn, prev, now)
        assert len(sql) == len(py), (prev, now)
        for a, b in zip(sql, py):
            assert a == b, f"{prev} -> {now}: SQL {a} != Python {b}"
        checked += len(py)
    store.close()
    assert checked > 0


@pytest.mark.skipif(not LIVE_DB.exists(), reason="no out/x.db")
def test_sql_changes_equal_python_changes_with_custom_thresholds(conn):
    """Thresholds are parameters on both sides; tighten them so every rule fires at least once."""
    store = Store(LIVE_DB)
    runs = df.runs_with_measurements(store)
    prev, now = runs[0], runs[-1]
    th = df.Thresholds(followers_pct=0.5, engagement_pp=0.05, engagement_rel_pct=5.0, views_rel_pct=5.0, silent_days=3)
    py = [asdict(c) for c in df.compare(store, run_prev=prev, run_now=now, th=th).changes]
    rows = conn.execute("SELECT * FROM mart.changes(%s, %s, %s, %s, %s, %s, %s)",
                        (prev, now, th.followers_pct, th.engagement_pp, th.engagement_rel_pct,
                         th.views_rel_pct, th.silent_days)).fetchall()
    sql = [dict(zip(COLUMNS, r)) for r in rows]
    store.close()
    assert sql == py
    assert any(r["flags"] for r in py), "thresholds this tight should flag something"


def test_v_changes_is_the_latest_pair(conn):
    prev, now = conn.execute(
        "SELECT (SELECT run_id FROM mart.v_runs WHERE recency = 2), (SELECT run_id FROM mart.v_runs WHERE recency = 1)"
    ).fetchone()
    view = conn.execute("SELECT * FROM mart.v_changes").fetchall()
    fn = conn.execute("SELECT * FROM mart.changes(%s, %s)", (prev, now)).fetchall()
    assert view == fn


# ------------------------------------------------------------- 5. rounding rule --
def test_round_half_up_matches_python_helper(conn):
    """mart.round_half_up must agree with diff._round on the values where Python's round()
    would not — exact halves — and on a random spread of realistic deltas."""
    cases = [0.125, -0.125, 2.675, 28.124999999999996, 0.00005, 12.5, 1e-9, 0.0]
    rnd = random.Random(20260919)
    for _ in range(300):
        base = rnd.randrange(1000, 300001, 100)
        delta = rnd.randrange(-5000, 5001, 100)
        cases.append(delta / base * 100)
    for places in (2, 4):
        got = conn.execute("SELECT mart.round_half_up(x, %s) FROM unnest(%s::float8[]) AS x",
                           (places, cases)).fetchall()
        for x, (g,) in zip(cases, got):
            assert g == df._round(x, places), (x, places, g, df._round(x, places))


def test_legacy_measured_at_is_range_end_not_import_time(conn):
    """core.measurements re-dates legacy rows to their measurement window; raw keeps the truth."""
    rows = conn.execute("""
        SELECT c.measured_at::date, c.range_end, c.recorded_at::date
          FROM core.measurements c WHERE c.source = 'legacy-import' LIMIT 20""").fetchall()
    if not rows:
        pytest.skip("no legacy rows loaded")
    for measured, range_end, recorded in rows:
        assert measured == range_end
        assert recorded != range_end or measured == recorded
