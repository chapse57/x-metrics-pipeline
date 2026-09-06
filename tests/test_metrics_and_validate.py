from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xmetrics.legacy import import_jsonl
from xmetrics.metrics import Post, rates_from_medians, select_posts, summarize, tier_for
from xmetrics.parse import PostCounts
from xmetrics.store import Store
from xmetrics.validate import check_against_csv, check_recompute, check_rules, run_all

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def _post(i, likes, replies=0, reposts=0, views=1000, **kw):
    return Post(status_id=str(i), posted_at=NOW - timedelta(days=i), counts=PostCounts(replies, reposts, likes, 0, views), **kw)


def test_tiers():
    assert tier_for(3_142) == "Micro (<25K)"
    assert tier_for(25_000) == "Mid (25-100K)"
    assert tier_for(100_000) == "Macro (100K+)"


def test_select_posts_excludes_reposts_and_pinned_and_orders_recent_first():
    posts = [_post(1, 10), _post(2, 20, is_repost=True), _post(3, 30, is_pinned=True), _post(4, 40), _post(5, 50)]
    sel = select_posts(posts, n=2)
    assert [p.status_id for p in sel] == ["1", "4"]


def test_summarize_uses_per_post_median_of_sum():
    # interactions per post: 10, 22, 34 -> median 22 ; views 1000,1000,1000
    posts = [_post(1, 10), _post(2, 20, replies=2), _post(3, 30, replies=2, reposts=2)]
    s = summarize(10_000, posts, now=NOW)
    assert s.engagement_rate == pytest.approx(0.22)
    assert s.views_to_followers == pytest.approx(10.0)
    assert s.posts_measured == 3 and s.range_start == "2026-09-03" and s.range_end == "2026-09-05"
    assert s.days_since_last_post == 1


def test_summarize_rejects_empty():
    with pytest.raises(ValueError):
        summarize(1000, [], now=NOW)


def test_rates_from_medians_matches_hand_sheet_definition():
    er, vr = rates_from_medians(3142, 21, 1, 0, 1238)
    assert round(er, 2) == 0.70 and round(vr, 1) == 39.4  # the values on the delivered sheet for @realFatCat1


# ---------------------------------------------------------------- legacy data, end to end --
@pytest.fixture(scope="module")
def legacy_store(tmp_path_factory):
    st = Store(tmp_path_factory.mktemp("db") / "x.db")
    import_jsonl(st, ROOT / "fixtures" / "legacy_m2_results.jsonl", as_of=NOW,
                 screened_csv=ROOT / "fixtures" / "legacy_screened_out.csv")
    return st


def test_legacy_import_count(legacy_store):
    assert len(legacy_store.latest_measurements()) == 95   # 148 measured - 53 screened out by hand
    assert legacy_store.pending_handles() == []  # resume set is empty after a full import


def test_legacy_recompute_has_no_errors(legacy_store):
    assert [i for i in check_recompute(legacy_store) if i.severity == "error"] == []


def test_legacy_matches_hand_made_master_csv(legacy_store):
    """The 95 rows the client received (measured by hand on 2026-09-05) must match the DB
    to the rounding shown on the sheet. Rows only in the CSV (5 M1 samples measured earlier,
    not in the JSONL) are reported as warnings, not errors."""
    issues = check_against_csv(legacy_store, ROOT / "fixtures" / "legacy_master.csv")
    errors = [i for i in issues if i.severity == "error"]
    missing = [i for i in issues if i.check == "csv.missing_in_db"]
    assert errors == []
    assert len(missing) == 5


def test_rules_flag_recency_but_not_block(legacy_store):
    issues = check_rules(legacy_store, {"max_days_since_post": 7})  # tighter than the client rule, to exercise the check
    kinds = {i.check for i in issues}
    assert "rules.recency" in kinds
    assert all(i.severity == "warn" for i in issues if i.check == "rules.recency")


def test_run_all_blocks_only_on_errors(tmp_path):
    st = Store(tmp_path / "x.db")
    run = st.start_run("test")
    st.upsert_account("@good", bio="", bio_url="site.com", dm_open=1, niche="Futures / order flow")
    st.save_measurement("@good", run, summarize(10_000, [_post(i, 10) for i in range(1, 16)], now=NOW))
    st.upsert_account("@nocontact", bio="", bio_url="", dm_open=0, niche="Other")
    st.save_measurement("@nocontact", run, summarize(10_000, [_post(i, 10) for i in range(1, 16)], now=NOW))
    issues = run_all(st)
    blocked = {i.handle for i in issues if i.severity == "error"}
    assert blocked == {"nocontact"}


def test_display_case_survives_normalized_updates(tmp_path):
    """Live-run regression: the collector updates by normalized handle; that must not
    lowercase the display name the account was registered with."""
    st = Store(tmp_path / "x.db")
    st.upsert_account("realFatCat1")
    st.upsert_account("realfatcat1", bio="x", dm_open=1)          # collector-style update
    assert st.get_account("realfatcat1")["display"] == "realFatCat1"
    st.upsert_account("realfatcat1", display="RealFatCat1")       # explicit display wins
    assert st.get_account("realfatcat1")["display"] == "RealFatCat1"
