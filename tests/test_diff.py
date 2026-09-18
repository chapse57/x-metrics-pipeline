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


def _run(store: Store, note: str, rows: dict[str, Summary], started_at: str) -> str:
    run_id = store.start_run("playwright", note=note)
    # make run order explicit and independent of wall-clock
    store.conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (started_at, run_id)); store.conn.commit()
    for h, s in rows.items():
        store.upsert_account(h)
        store.save_measurement(h, run_id, s)
    store.finish_run(run_id)
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
    }, "2026-09-14T00:00:00+00:00")
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
    assert "## Not measured this run" in txt and "@gone" in txt
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
