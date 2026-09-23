"""Two runs in, one change report out. Every threshold is exercised on both sides of the line,
because a flag that fires too easily is noise and one that never fires is a missing alarm."""
import json
import sqlite3
from pathlib import Path

import pytest

from xmetrics import diff as df
from xmetrics.cli import main
from xmetrics.metrics import Summary, tier_for
from xmetrics.store import Store


def _summary(followers, er, vr, days=1, posts=20):
    return Summary(followers=followers, posts_measured=posts, range_start="2026-09-01", range_end="2026-09-06",
                   median_likes=1, median_replies=1, median_reposts=1, median_views=100,
                   engagement_rate=er, views_to_followers=vr, tier=tier_for(followers), days_since_last_post=days)


def _run(store: Store, note: str, rows: dict[str, Summary], started_at: str, *,
         missing: list[str] = (), error: list[str] = (), pending: list[str] = ()) -> str:
    """A run that measured `rows`, and also went looking for `missing` (not there), `error`
    (there, unreadable) and `pending` (never reached) accounts."""
    run_id = store.start_run("playwright", note=note, targets=[*rows, *missing, *error, *pending])
    for h, s in rows.items():
        store.upsert_account(h)
        store.save_measurement(h, run_id, s)
    for h in missing:
        store.mark_target(run_id, h, "missing", "account missing/suspended")
    for h in error:
        store.mark_target(run_id, h, "error", "followers not found (markup changed?)")
    store.finish_run(run_id)
    # make run and measurement order explicit and independent of wall-clock
    store.conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (started_at, run_id))
    store.conn.execute("UPDATE measurements SET measured_at=? WHERE run_id=?", (started_at, run_id))
    store.conn.commit()
    return run_id


@pytest.fixture
def two_runs(tmp_path):
    st = Store(tmp_path / "x.db")
    a = _run(st, "week 1", {
        "steady":   _summary(10_000, 1.00, 30.0),
        "growing":  _summary(24_000, 1.00, 30.0),           # +8% followers, crosses into Mid tier
        "fading":   _summary(10_000, 1.00, 30.0, days=3),   # engagement halves, goes silent
        "quiet":    _summary(10_000, 0.30, 30.0),           # tiny absolute move, big relative: NOT flagged
        "gone":     _summary(5_000, 2.00, 50.0),            # dropped in week 2
    }, "2026-09-07T00:00:00+00:00")
    b = _run(st, "week 2", {
        "steady":   _summary(10_100, 1.02, 31.0),           # +1% / +0.02pp / +3%: within thresholds
        "growing":  _summary(26_000, 1.00, 30.0),
        "fading":   _summary(10_000, 0.50, 30.0, days=20),
        "quiet":    _summary(10_000, 0.42, 30.0),           # +0.12pp (<0.2pp) even though +40% relative
        "newbie":   _summary(3_000, 3.00, 80.0),            # new in week 2
    }, "2026-09-14T00:00:00+00:00", missing=["gone"])      # went looking for "gone": not there
    return st, a, b


def test_compare_picks_latest_two_runs_and_classifies(two_runs):
    st, a, b = two_runs
    res = df.compare(st)
    assert (res.run_prev, res.run_now) == (a, b)
    kinds = {c.handle: c.kind for c in res.changes}
    assert kinds["newbie"] == "new" and kinds["gone"] == "dropped"
    assert kinds["steady"] == "changed" and kinds["growing"] == "changed"


def test_flags_fire_on_the_right_side_of_each_threshold(two_runs):
    st, _, _ = two_runs
    by = {c.handle: c for c in df.compare(st).changes}
    assert by["steady"].flags == []                                  # +1% followers, +0.02pp: nothing
    assert by["growing"].flags == ["followers_up", "tier_change"]    # 24k -> 26k = +8.3%, Micro -> Mid
    assert by["growing"].followers_delta_pct == pytest.approx(8.33, abs=0.01)
    assert set(by["fading"].flags) == {"engagement_down", "went_silent"}
    assert by["fading"].engagement_delta_pp == pytest.approx(-0.5)
    assert by["quiet"].flags == []                                   # 0.12pp < 0.2pp guard, despite +40% relative


def test_thresholds_are_tunable(two_runs):
    st, _, _ = two_runs
    loose = df.Thresholds(followers_pct=0.5, engagement_pp=0.01, engagement_rel_pct=1.0, views_rel_pct=2.0)
    by = {c.handle: c for c in df.compare(st, th=loose).changes}
    assert "followers_up" in by["steady"].flags and "views_up" in by["steady"].flags
    assert "engagement_up" in by["quiet"].flags


def test_a_run_says_nothing_about_accounts_it_did_not_try(tmp_path):
    """Week 2 measured a subset. "b" was not tried, so it is neither dropped nor listed."""
    st = Store(tmp_path / "x.db")
    _run(st, "week 1", {"a": _summary(1_000, 1.0, 10.0), "b": _summary(2_000, 1.0, 10.0)}, "2026-09-07T00:00:00+00:00")
    _run(st, "week 2", {"a": _summary(1_100, 1.0, 10.0), "c": _summary(3_000, 1.0, 10.0)}, "2026-09-14T00:00:00+00:00")
    res = df.compare(st)
    assert res.mode == "baseline" and res.complete
    assert {c.handle: c.kind for c in res.changes} == {"a": "changed", "c": "new"}   # "b" is not listed at all
    assert "0 dropped" in df.summary_line(res) and "tried 2: 2 measured" in df.summary_line(res)


def test_dropped_means_we_went_looking_and_it_was_not_there(tmp_path):
    """Three ways to come back without a number, three different meanings. Only 'missing' —
    the account itself is gone — becomes a change. A failed read or an unreached account is
    the collector's problem and is reported as such, never as the account's."""
    st = Store(tmp_path / "x.db")
    _run(st, "week 1", {h: _summary(1_000, 1.0, 10.0) for h in "abcd"}, "2026-09-07T00:00:00+00:00")
    r = _run(st, "week 2", {"a": _summary(1_000, 1.0, 10.0)}, "2026-09-14T00:00:00+00:00",
             missing=["b"], error=["c"], pending=["d"])
    res = df.compare(st)
    assert res.run_now == r
    assert {c.handle: c.kind for c in res.changes} == {"a": "unchanged", "b": "dropped"}
    assert res.failed == ["c"] and res.not_reached == ["d"] and not res.complete
    assert res.targets == {"pending": 1, "measured": 1, "missing": 1, "error": 1}
    text = df.report(res)
    assert "went looking, account not there" in text and "@b" in text
    assert "never reached (1)" in text and "@d" in text and "could not be read (1)" in text and "@c" in text
    payload = df.as_json(res)
    assert payload["complete"] is False and payload["not_reached"] == ["d"] and payload["failed"] == ["c"]
    # a missing account with no earlier measurement is nothing to report — there was nothing to lose
    r2 = _run(st, "week 3", {}, "2026-09-21T00:00:00+00:00", missing=["zzz"])
    assert df.compare(st, run_now=r2).changes == [] if r2 in df.runs_with_measurements(st) else True


def test_baseline_is_the_accounts_own_previous_measurement_not_the_previous_run(tmp_path):
    """Week 3 re-measures only "b", which week 2 skipped: its baseline is week 1, not "new"."""
    st = Store(tmp_path / "x.db")
    a = _run(st, "week 1", {"a": _summary(1_000, 1.0, 10.0), "b": _summary(2_000, 1.0, 10.0)}, "2026-09-07T00:00:00+00:00")
    b = _run(st, "week 2", {"a": _summary(1_100, 1.0, 10.0)}, "2026-09-14T00:00:00+00:00")
    c = _run(st, "week 3", {"b": _summary(2_400, 1.0, 10.0)}, "2026-09-21T00:00:00+00:00")
    res = df.compare(st)
    assert res.run_now == c and res.prev_runs == [a] and res.run_prev == a
    (only,) = res.changes
    assert only.handle == "b" and only.kind == "changed" and only.followers_delta == 400 and "followers_up" in only.flags
    # a run touching both: baselines come from two different runs, and the report says so
    d = _run(st, "week 4", {"a": _summary(1_100, 1.0, 10.0), "b": _summary(2_400, 1.0, 10.0)}, "2026-09-28T00:00:00+00:00")
    res = df.compare(st)
    assert res.run_now == d and res.prev_runs == [b, c] and res.run_prev is None
    assert {c.handle: c.kind for c in res.changes} == {"a": "unchanged", "b": "unchanged"}
    assert "each account's previous measurement" in df.report(res)
    # pair mode is still available for "exactly these two runs", whole sets, absence on both sides reported
    pair = df.compare(st, run_prev=a, run_now=c)
    assert pair.mode == "pair" and {x.handle: x.kind for x in pair.changes} == {"a": "dropped", "b": "changed"}


def test_baseline_is_by_measurement_time_not_run_start(tmp_path):
    """The 2026-09 legacy import ran on 09-07 but carries numbers taken by hand up to 09-05. A live
    run on 09-06 sits between the two dates: by run start the import is newer, by measurement it is
    older. The baseline goes by measurement, the same way core.measurements re-dates those rows."""
    st = Store(tmp_path / "x.db")
    live1 = _run(st, "live", {"a": _summary(1_000, 1.0, 10.0)}, "2026-09-06T03:19:21+00:00")
    imp = st.start_run("legacy-import", note="hand list", targets=["a"])
    st.save_measurement("a", imp, Summary(followers=900, posts_measured=20, range_start="2026-08-21", range_end="2026-09-05",
                                          median_likes=1, median_replies=1, median_reposts=1, median_views=100,
                                          engagement_rate=1.0, views_to_followers=10.0, tier=tier_for(900), days_since_last_post=1))
    st.conn.execute("UPDATE runs SET started_at='2026-09-07T02:24:51+00:00' WHERE run_id=?", (imp,))
    st.conn.execute("UPDATE measurements SET measured_at='2026-09-07T02:24:51+00:00' WHERE run_id=?", (imp,)); st.conn.commit()
    live2 = _run(st, "live", {"a": _summary(1_100, 1.0, 10.0)}, "2026-09-18T02:07:45+00:00")
    assert df.runs_with_measurements(st) == [imp, live1, live2]          # runs ordered by when their numbers were taken
    (c,) = df.compare(st, run_now=live2).changes
    assert c.run_prev == live1 and c.followers_prev == 1_000             # baselines the same way
    (c,) = df.compare(st, run_now=live1).changes
    assert c.run_prev == imp and c.followers_prev == 900                 # 09-05 hand numbers precede the 09-06 run
    assert [x.kind for x in df.compare(st, run_now=imp).changes] == ["new"]   # nothing was taken before 09-05


def test_a_screened_out_account_that_was_not_tried_is_not_dropped(tmp_path):
    st = Store(tmp_path / "x.db")
    _run(st, "week 1", {"a": _summary(1_000, 1.0, 10.0), "s": _summary(9, 1.0, 1.0)}, "2026-09-07T00:00:00+00:00")
    st.set_status("s", "screened_out", "spam")             # deliberately excluded: next run will not try it
    _run(st, "week 2", {"a": _summary(1_000, 1.0, 10.0)}, "2026-09-14T00:00:00+00:00")
    res = df.compare(st)
    assert {c.handle: c.kind for c in res.changes} == {"a": "unchanged"}
    # ...but if a run does try it and it is gone, that is a fact worth reporting
    _run(st, "week 3", {"a": _summary(1_000, 1.0, 10.0)}, "2026-09-21T00:00:00+00:00", missing=["s"])
    assert {c.handle: c.kind for c in df.compare(st).changes} == {"a": "unchanged", "s": "dropped"}


def test_a_run_declares_its_targets_and_a_dead_run_leaves_them_pending(tmp_path):
    st = Store(tmp_path / "x.db")
    r = st.start_run("playwright", targets=["@A", "b"])
    assert st.run_targets(r) == {"a": "pending", "b": "pending"}
    st.upsert_account("a"); st.save_measurement("a", r, _summary(1_000, 1.0, 10.0))
    assert st.run_targets(r) == {"a": "measured", "b": "pending"}       # the run died before "b"
    st.mark_target(r, "c", "error", "late addition")                    # a handle named mid-run is registered
    assert st.run_targets(r)["c"] == "error"
    with pytest.raises(ValueError):
        st.mark_target(r, "b", "vanished")


def test_old_store_files_get_run_targets_reconstructed_from_measurements(tmp_path):
    """A file from before run_targets (and one with the short-lived runs.scope column) opens
    cleanly: targets = what was measured, all 'measured', scope gone. Nothing is invented."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE accounts (handle TEXT PRIMARY KEY, display TEXT, bio TEXT, bio_url TEXT, dm_open INTEGER, niche TEXT,
        niche_source TEXT, fit_note TEXT, hook_link TEXT, hook_note TEXT, status TEXT NOT NULL DEFAULT 'pending',
        screen_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, source TEXT NOT NULL, note TEXT,
        scope TEXT NOT NULL DEFAULT 'full');
      CREATE TABLE measurements (handle TEXT NOT NULL, run_id TEXT NOT NULL, measured_at TEXT NOT NULL, followers INTEGER NOT NULL,
        posts_measured INTEGER NOT NULL, range_start TEXT, range_end TEXT, median_likes REAL, median_replies REAL,
        median_reposts REAL, median_views REAL, engagement_rate REAL NOT NULL, views_to_followers REAL NOT NULL,
        tier TEXT NOT NULL, days_since_last_post INTEGER, PRIMARY KEY (handle, run_id));
      INSERT INTO accounts (handle, status, created_at, updated_at) VALUES ('a', 'measured', 't', 't'), ('b', 'measured', 't', 't');
      INSERT INTO runs VALUES ('r1', '2026-09-07T00:00:00+00:00', NULL, 'playwright', NULL, 'full');
      INSERT INTO measurements VALUES ('a', 'r1', '2026-09-07T00:00:01+00:00', 1000, 20, NULL, NULL, 1, 1, 1, 100, 1.0, 10.0, 'Micro (<25K)', 1);
    """); con.commit(); con.close()
    st = Store(db)
    assert "scope" not in {r["name"] for r in st.conn.execute("PRAGMA table_info(runs)")}
    assert st.run_targets("r1") == {"a": "measured"}                    # "b" was not measured: we do not claim it was tried
    assert st.conn.execute("SELECT detail FROM run_targets").fetchone()[0] == Store.BACKFILL_NOTE
    Store(db)                                                           # opening again changes nothing
    assert st.conn.execute("SELECT count(*) FROM run_targets").fetchone()[0] == 1


def test_one_run_is_not_a_comparison(tmp_path):
    st = Store(tmp_path / "x.db")
    _run(st, "only", {"a": _summary(1_000, 1.0, 10.0)}, "2026-09-07T00:00:00+00:00")
    res = df.compare(st)
    assert res.run_prev is None and res.changes == []
    assert "nothing to compare" in df.summary_line(res)


def test_explicit_runs_and_unknown_run_rejected(two_runs):
    st, a, b = two_runs
    assert df.compare(st, run_prev=a, run_now=b).run_now == b
    with pytest.raises(ValueError):
        df.compare(st, run_prev="20990101T000000Z-playwright", run_now=b)


def test_report_orders_dropped_new_flagged_and_lists_the_rest(two_runs):
    st, _, _ = two_runs
    res = df.compare(st)
    assert [c.handle for c in res.changes][:2] == ["gone", "newbie"]
    flagged = [c.handle for c in res.flagged]
    assert set(flagged) == {"growing", "fading"} and flagged[0] == "growing"   # bigger follower move first
    txt = df.report(res)
    assert "2 flagged | 1 new | 1 dropped | 2 unchanged of 6" in txt
    assert "@growing" in txt and "Micro (<25K) → Mid (25-100K)" in txt
    assert "## Dropped — went looking" in txt and "@gone" in txt
    assert "## Within thresholds (2)" in txt and "@quiet" in txt


def test_cli_writes_files_and_fail_on_flags(two_runs, tmp_path, capsys):
    st, _, _ = two_runs
    st.close()
    db = str(tmp_path / "x.db"); out = tmp_path / "out"
    assert main(["--db", db, "diff", "--out", str(out)]) == 0
    assert (out / "changes.md").exists()
    payload = json.loads((out / "changes.json").read_text(encoding="utf-8"))
    assert payload["counts"] == {"flagged": 2, "new": 1, "dropped": 1, "unchanged": 2, "total": 6}
    assert main(["--db", db, "diff", "--out", str(out), "--fail-on-flags"]) == 1
    # loosen every tunable threshold: only the tier crossing survives (a tier change is a fact, not a threshold)
    assert main(["--db", db, "diff", "--out", str(out), "--followers-pct", "50", "--engagement-pp", "5",
                 "--silent-days", "100", "--fail-on-flags"]) == 1
    payload = json.loads((out / "changes.json").read_text(encoding="utf-8"))
    assert [c["flags"] for c in payload["changes"] if c["flags"]] == [["tier_change"]]
    assert "flagged" in capsys.readouterr().out

def test_rounding_matches_postgres_half_away_from_zero():
    """v_changes rounds with Postgres's round(numeric): half away from zero. Python's round() is
    half-to-even on the binary value and would disagree on exactly these inputs."""
    assert df._round(0.125, 2) == 0.13 and round(0.125, 2) == 0.12
    assert df._round(-0.125, 2) == -0.13
    assert df._round(2.675, 2) == 2.68 and round(2.675, 2) == 2.67
    assert df._round(0.00005, 4) == 0.0001
    assert df._rel(1, 8) == 12.5 and df._rel(1, 3) == 33.33
