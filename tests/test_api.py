"""The read API over a real database: response shapes, filters and paging, 404s, the health
rule, and — the one that matters — that the role the API connects as cannot write.

Needs XMETRICS_PG_DSN (owner: to migrate, load and create the reader login) like
tests/test_pg.py. The reader DSN is derived from it. Skipped without a server.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

DSN = os.environ.get("XMETRICS_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="XMETRICS_PG_DSN not set")

ROOT = Path(__file__).resolve().parent.parent
SQLITE_FILES = [p for p in (ROOT / "out" / "x_legacy_claude.db", ROOT / "out" / "x.db") if p.exists()]
READER_USER, READER_PW = "xmetrics_api_test", "readonly-test"


def reader_dsn(owner_dsn: str) -> str:
    """Same server and database as the owner DSN, different login."""
    import psycopg.conninfo as ci
    parts = ci.conninfo_to_dict(owner_dsn)
    parts["user"], parts["password"] = READER_USER, READER_PW
    return ci.make_conninfo(**parts)


@pytest.fixture(scope="session")
def database():
    """Fresh schemas, real data loaded, reader login created. The API's DSN is set for the session."""
    from pg.load import load
    from pg.migrate import migrate
    from pg.roles import ensure_reader
    with psycopg.connect(DSN) as conn:
        conn.execute("DROP SCHEMA IF EXISTS raw, core, mart, pg CASCADE")
        conn.commit()
        migrate(conn)
        for path in SQLITE_FILES:
            load(conn, path)
        ensure_reader(conn, READER_USER, READER_PW)
    os.environ["XMETRICS_API_DSN"] = reader_dsn(DSN)
    yield reader_dsn(DSN)


@pytest.fixture(scope="session")
def client(database):
    from api.main import app
    return TestClient(app)


# --------------------------------------------------------------- 1. read-only role --
def test_reader_role_cannot_write(database):
    """The claim behind the whole API. Not a code review — the database says no."""
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")     # take away the belt; the braces must hold
        assert conn.execute("SELECT count(*) FROM mart.v_latest").fetchone()[0] > 0
        for stmt in (
            "INSERT INTO raw.runs (run_id, started_at, source, source_db) VALUES ('evil', now(), 'x', 'x')",
            "UPDATE raw.measurements SET followers = 0",
            "DELETE FROM raw.run_targets",
            "CREATE TABLE mart.scratch (x int)",
            "INSERT INTO pg.schema_migrations (filename, applied_at) VALUES ('evil', now())",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


# ------------------------------------------------------------- 2. response schema --
def test_accounts_schema_and_default_sort(client):
    r = client.get("/accounts")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"total", "limit", "offset", "sort", "items"} and body["sort"] == "engagement"
    assert body["total"] >= 3 and len(body["items"]) == min(body["total"], 100)
    first = body["items"][0]
    assert set(first) == {"handle", "display", "niche", "status", "dm_open", "run_id", "source", "measured_at",
                          "followers", "tier", "engagement_rate", "views_to_followers", "posts_measured",
                          "days_since_last_post"}
    rates = [a["engagement_rate"] for a in body["items"]]
    assert rates == sorted(rates, reverse=True)
    assert first["measured_at"].endswith("Z")                       # UTC, whatever the server's zone


# ---------------------------------------------------------------- 3. filters + sort --
def test_accounts_filters_and_sort(client):
    everyone = client.get("/accounts", params={"limit": 500}).json()["items"]
    mid = client.get("/accounts", params={"tier": "Mid (25-100K)", "limit": 500}).json()
    assert mid["total"] == sum(1 for a in everyone if a["tier"] == "Mid (25-100K)") and mid["total"] > 0
    assert all(a["tier"] == "Mid (25-100K)" for a in mid["items"])
    hot = client.get("/accounts", params={"min_engagement": 0.5, "limit": 500}).json()
    assert hot["total"] == sum(1 for a in everyone if a["engagement_rate"] >= 0.5)
    assert all(a["engagement_rate"] >= 0.5 for a in hot["items"])
    by_followers = client.get("/accounts", params={"sort": "-followers", "limit": 5}).json()["items"]
    assert [a["followers"] for a in by_followers] == sorted(a["followers"] for a in by_followers)
    assert client.get("/accounts", params={"sort": "followers; drop table raw.runs"}).status_code == 422
    assert client.get("/accounts", params={"tier": "Huge"}).status_code == 422


# ----------------------------------------------------------------------- 4. paging --
def test_accounts_paging_is_stable_and_complete(client):
    total = client.get("/accounts", params={"limit": 1}).json()["total"]
    seen = []
    for offset in range(0, total, 7):
        page = client.get("/accounts", params={"sort": "handle", "limit": 7, "offset": offset}).json()
        assert page["total"] == total and page["offset"] == offset
        seen += [a["handle"] for a in page["items"]]
    assert len(seen) == total and len(set(seen)) == total           # every account once, none twice
    assert client.get("/accounts", params={"limit": 0}).status_code == 422
    assert client.get("/accounts", params={"limit": 501}).status_code == 422


# -------------------------------------------------------------- 5. one account / 404 --
def test_account_detail_history_and_404(client):
    r = client.get("/accounts/@RealFatCat1")                        # case and '@' are the client's problem, not ours
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == "realfatcat1" and body["display"] == "realFatCat1"
    hist = body["history"]
    assert len(hist) >= 3                                           # legacy + two live runs
    assert [h["measured_at"] for h in hist] == sorted(h["measured_at"] for h in hist)
    assert hist[0]["source"] == "legacy-import"                     # re-dated to its window: before the 09-06 live run
    assert body["run_id"] == hist[-1]["run_id"] and body["followers"] == hist[-1]["followers"]
    assert client.get("/accounts/nobody_here").status_code == 404


# ------------------------------------------------------------------------ 6. changes --
def test_changes_is_the_dashboard_view_and_reports_no_untried_account_as_dropped(client, database):
    r = client.get("/changes")
    assert r.status_code == 200
    body = r.json()
    with psycopg.connect(database) as conn:
        latest = conn.execute("SELECT run_id FROM mart.v_runs WHERE recency = 1").fetchone()[0]
        n_view = conn.execute("SELECT count(*) FROM mart.v_changes").fetchone()[0]
    assert body["run"] == latest and body["counts"]["total"] == n_view == len(body["changes"])
    assert body["counts"]["dropped"] == 0                           # 3 live accounts after a 95-account import
    kinds = {c["kind"] for c in body["changes"]}
    assert kinds <= {"new", "dropped", "changed", "unchanged"}
    assert client.get("/changes", params={"run": latest}).json() == body
    assert client.get("/changes", params={"run": "no-such-run"}).status_code == 404


# --------------------------------------------------------------------- 7. runs + health --
def test_runs_and_health(client, database, monkeypatch):
    runs = client.get("/runs").json()
    assert runs and all(r["complete"] for r in runs)                # reconstructed runs: nothing unreached
    assert [r["taken_at"] for r in runs] == sorted((r["taken_at"] for r in runs), reverse=True)
    legacy = next(r for r in runs if r["source"] == "legacy-import")
    assert legacy["targets"] == legacy["measured"] == 95

    h = client.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["database"] == "ok" and body["latest_run"] == runs[0]["run_id"]
    assert body["stale_after_days"] == 8 and body["latest_run_complete"] is True
    taken = datetime.fromisoformat(body["data_taken_at"].replace("Z", "+00:00"))
    expect_stale = datetime.now(timezone.utc) - taken > timedelta(days=8)
    assert body["stale"] is expect_stale and body["ok"] is (not expect_stale)

    # the database going away is a 503 with the reason, not a 500 with a traceback
    monkeypatch.setenv("XMETRICS_API_DSN", "postgresql://nobody:nothing@127.0.0.1:1/none?connect_timeout=1")
    down = client.get("/health")
    assert down.status_code == 503 and down.json()["ok"] is False and "error" in down.json()["database"]
