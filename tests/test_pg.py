"""PostgreSQL layer: migration idempotence, load counts, reload invariance, and the one
that matters — the change report computed in SQL (mart.changes, mart.changes_since,
mart.v_changes) equals xmetrics.diff.compare() in Python, every row, every column, for
every run in the real database.

Needs a reachable PostgreSQL and XMETRICS_PG_DSN (any database; the raw/core/mart/pg
schemas are dropped and rebuilt at the start of the session). Skipped otherwise, so the
rest of the suite runs without a server.

    XMETRICS_PG_DSN=postgresql://postgres:pw@localhost:5432/xmetrics python -m pytest tests/test_pg.py -q
"""
from __future__ import annotations

import os
import random
import sqlite3
from dataclasses import asdict, fields
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
    """A second migrate applies nothing and changes nothing; every file is in the ledger once."""
    from pg.migrate import migrate, migration_files
    before = object_inventory(conn)
    assert migrate(conn) == []
    assert object_inventory(conn) == before
    ledger = conn.execute("SELECT filename, runs FROM pg.schema_migrations ORDER BY 1").fetchall()
    assert ledger == [(p.name, 1) for p in migration_files()]
    assert {"002_run_scope.sql", "004_run_targets.sql"} <= {f for f, _ in ledger}


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
COLUMNS = [f.name for f in fields(df.Change)]     # mart.change is this dataclass, field for field


def sql_changes(conn, run_prev: str, run_now: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM mart.changes(%s, %s)", (run_prev, run_now)).fetchall()
    return [dict(zip(COLUMNS, r)) for r in rows]


def merged_store(tmp_path) -> Store:
    """One SQLite file holding every out/*.db, merged the way pg/load.py merges them (runs and
    measurements keyed by run, accounts by handle with the newer updated_at winning), so Python
    can compute the baseline report over the same data Postgres holds."""
    st = Store(tmp_path / "merged.db")
    rows = {t: [] for t in ("runs", "accounts", "measurements", "run_targets")}
    for path in SQLITE_FILES:
        src = Store(path)                       # opening upgrades an old file (reconstructs run_targets)
        for t in rows:
            rows[t] += [dict(r) for r in src.conn.execute(f"SELECT * FROM {t}")]
        src.close()
    rows["accounts"].sort(key=lambda a: a["updated_at"])
    with st.tx() as c:
        for t in ("runs", "accounts", "measurements", "run_targets"):
            for r in rows[t]:
                cols = ", ".join(r); marks = ", ".join("?" * len(r))
                c.execute(f"INSERT OR REPLACE INTO {t} ({cols}) VALUES ({marks})", list(r.values()))
    return st


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


@pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load")
def test_sql_baseline_changes_equal_python_for_every_run(conn, tmp_path):
    """The rule the dashboard reads — each account vs its own previous measurement, dropped only
    when it was looked for and missing — computed in SQL over the merged database equals Python
    over a merged copy."""
    from pg.load import load
    for path in SQLITE_FILES:
        load(conn, path)
    st = merged_store(tmp_path)
    runs = df.runs_with_measurements(st)
    assert len(runs) >= 2
    checked = 0
    for r in runs:
        py = [asdict(c) for c in df.compare(st, run_now=r).changes]
        sql = [dict(zip(COLUMNS, x)) for x in conn.execute("SELECT * FROM mart.changes_since(%s)", (r,)).fetchall()]
        assert len(sql) == len(py), r
        for a, b in zip(sql, py):
            assert a == b, f"{r}: SQL {a} != Python {b}"
        checked += len(py)
    st.close()
    assert checked > 0


@pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load")
def test_v_changes_is_the_latest_run_against_baselines(conn, tmp_path):
    latest, complete = conn.execute("SELECT run_id, complete FROM mart.v_runs WHERE recency = 1").fetchone()
    view = conn.execute("SELECT * FROM mart.v_changes").fetchall()
    fn = conn.execute("SELECT * FROM mart.changes_since(%s)", (latest,)).fetchall()
    assert view == fn and view
    st = merged_store(tmp_path)
    py = df.compare(st)
    st.close()
    assert py.run_now == latest and py.complete == complete
    assert [dict(zip(COLUMNS, r)) for r in view] == [asdict(c) for c in py.changes]
    # the bug 002 and 004 exist for: on the real data (a 3-account live run after a 95-account
    # import) nothing is "dropped", because nobody went looking for the other 92
    assert not [r for r in view if r[COLUMNS.index("kind")] == "dropped"]


def test_run_targets_are_loaded_or_reconstructed_and_constrained(conn):
    """Every run with measurements has targets; files from before run_targets get them
    reconstructed (targets = measured) exactly as xmetrics.store does, so both sides agree."""
    from xmetrics.store import Store as S
    # reconstructed targets carry BACKFILL_NOTE in `detail`; targets the collector recorded itself carry
    # NULL — hence IS NOT DISTINCT FROM, so the flag is True/False per run and never NULL
    rows = conn.execute("""
        SELECT r.run_id, count(m.handle), count(t.handle) FILTER (WHERE t.outcome = 'measured'),
               bool_and(t.detail IS NOT DISTINCT FROM %s)
        FROM raw.runs r
        JOIN raw.measurements m ON m.run_id = r.run_id
        LEFT JOIN raw.run_targets t ON t.run_id = r.run_id AND t.handle = m.handle
        GROUP BY r.run_id""", (S.BACKFILL_NOTE,)).fetchall()
    assert rows
    for run_id, measured, targeted, reconstructed in rows:
        assert measured == targeted, run_id                       # every measurement has a 'measured' target
        assert reconstructed in (True, False), run_id            # all-or-nothing per run: never a mix
    status = {r[0]: r for r in conn.execute(
        "SELECT run_id, targets, measured, missing, failed, not_reached, complete FROM mart.v_run_status").fetchall()}
    for run_id, *_ in rows:
        assert status[run_id][6] is True                          # nothing pending: a reconstructed run is complete
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("INSERT INTO raw.run_targets (run_id, handle, outcome, updated_at) "
                         "VALUES (%s, 'x', 'vanished', now())", (rows[0][0],))
    assert "scope" not in {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='raw' AND table_name='runs'")}


def test_missing_failed_and_unreached_targets_mean_three_different_things(conn, tmp_path):
    """A synthetic history with every outcome, loaded into the same database: SQL and Python
    agree row for row, only 'missing' becomes 'dropped', and the run status says the run was
    incomplete. Cleaned up afterwards so the real-data tests are unaffected."""
    from pg.load import load
    from tests.test_diff import _run, _summary
    st = Store(tmp_path / "synthetic.db")
    r1 = _run(st, "s1", {f"syn_{h}": _summary(1_000, 1.0, 10.0) for h in "abcd"}, "2026-01-01T00:00:00+00:00")
    r2 = _run(st, "s2", {"syn_a": _summary(1_000, 1.0, 10.0)}, "2026-01-08T00:00:00+00:00",
              missing=["syn_b"], error=["syn_c"], pending=["syn_d"])
    r3 = _run(st, "s3", {"syn_d": _summary(1_200, 1.0, 10.0)}, "2026-01-15T00:00:00+00:00",   # d's baseline is r1
              missing=["syn_zzz"])                                                            # never measured: not reported
    try:
        res = load(conn, st.path)
        assert res.targets == 4 + 4 + 2
        for r in (r1, r2, r3):
            py = [asdict(c) for c in df.compare(st, run_now=r).changes]
            sql = [dict(zip(COLUMNS, x)) for x in conn.execute("SELECT * FROM mart.changes_since(%s)", (r,)).fetchall()]
            assert sql == py, r
        kinds = {c.handle: c.kind for c in df.compare(st, run_now=r2).changes}
        assert kinds == {"syn_a": "unchanged", "syn_b": "dropped"}
        status = conn.execute("SELECT targets, measured, missing, failed, not_reached, complete "
                              "FROM mart.v_run_status WHERE run_id = %s", (r2,)).fetchone()
        assert status == (4, 1, 1, 1, 1, False)
        (d,) = df.compare(st, run_now=r3).changes
        assert d.handle == "syn_d" and d.run_prev == r1 and d.followers_delta == 200
    finally:
        st.close()
        with conn.transaction():
            for t in ("run_targets", "measurements", "posts"):
                conn.execute(f"DELETE FROM raw.{t} WHERE run_id IN (%s, %s, %s)", (r1, r2, r3))
            conn.execute("DELETE FROM raw.runs WHERE run_id IN (%s, %s, %s)", (r1, r2, r3))
            conn.execute("DELETE FROM raw.accounts WHERE handle LIKE 'syn\\_%%'")


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
