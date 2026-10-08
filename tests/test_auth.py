"""API keys and the MCP tools, on a real database with real data.

  - no key / unknown key / revoked key -> 401; over the per-minute limit -> 429
  - only the SHA-256 of a key is stored; every decision is logged
  - the API's login still cannot write: not the keys table, not the log
  - MCP tools: same key check (surface 'mcp'), answers cut at the cap and saying so

Needs XMETRICS_PG_DSN, like tests/test_api.py (whose fixtures this reuses).
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_api import DSN, SQLITE_FILES, database  # noqa: E402,F401

pytestmark = [pytest.mark.skipif(not DSN, reason="XMETRICS_PG_DSN not set"),
              pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load")]


@pytest.fixture
def keys(database):
    from pg.roles import create_key, revoke_key
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM auth.request_log")
        conn.commit()
        good = create_key(conn, "test-good", per_minute=1000)
        tight = create_key(conn, "test-tight", per_minute=3)
        gone = create_key(conn, "test-revoked")
        revoke_key(conn, "test-revoked")
    return {"good": good, "tight": tight, "revoked": gone}


def log_rows():
    with psycopg.connect(DSN) as conn:
        return conn.execute("SELECT k.name, l.surface, l.path, l.decision FROM auth.request_log l "
                            "LEFT JOIN auth.api_key k USING (key_id) ORDER BY l.id").fetchall()


def test_no_key_unknown_key_revoked_key_are_401(keys):
    from api.main import app
    c = TestClient(app)
    assert c.get("/accounts").status_code == 401
    assert c.get("/accounts", headers={"X-API-Key": "xm_not-a-key"}).status_code == 401
    assert c.get("/runs", headers={"X-API-Key": keys["revoked"]}).status_code == 401
    assert c.get("/accounts", headers={"X-API-Key": keys["good"]}).status_code == 200
    assert c.get("/health").status_code in (200, 503)          # monitoring stays open
    assert [r[3] for r in log_rows()] == ["missing", "unknown", "revoked", "ok"]


def test_per_key_limit_is_enforced_and_logged(keys):
    from api.main import app
    c = TestClient(app, headers={"X-API-Key": keys["tight"]})
    codes = [c.get("/runs").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert c.get("/runs").headers["retry-after"] == "60"
    other = TestClient(app, headers={"X-API-Key": keys["good"]})
    assert other.get("/runs").status_code == 200                # one key's limit is not another's


def test_only_the_hash_is_stored(keys):
    with psycopg.connect(DSN) as conn:
        hashes = [r[0] for r in conn.execute("SELECT key_hash FROM auth.api_key")]
    assert all(len(h) == 64 for h in hashes)
    assert not set(keys.values()) & set(hashes)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM auth.api_key",
    "INSERT INTO auth.api_key (name, key_hash) VALUES ('x', repeat('a', 64))",
    "INSERT INTO auth.request_log (surface, path, decision) VALUES ('api', '/', 'ok')",
    "DELETE FROM auth.request_log",
    "UPDATE auth.api_key SET revoked_at = NULL",
])
def test_api_login_cannot_read_keys_or_touch_the_log(database, sql):
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")     # take away the easy refusal
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql)


def test_mcp_tools_check_the_key(keys, monkeypatch):
    from api import mcp_tools
    monkeypatch.setenv("XMETRICS_MCP_DSN", os.environ["XMETRICS_API_DSN"])
    monkeypatch.delenv("XMETRICS_MCP_KEY", raising=False)
    with pytest.raises(mcp_tools.Denied):
        mcp_tools.search_accounts()
    monkeypatch.setenv("XMETRICS_MCP_KEY", keys["revoked"])
    with pytest.raises(mcp_tools.Denied):
        mcp_tools.changes()
    monkeypatch.setenv("XMETRICS_MCP_KEY", keys["good"])
    assert mcp_tools.account("ProbableChris")["account"]["handle"] == "probablechris"
    assert [(r[1], r[2], r[3]) for r in log_rows()] == [
        ("mcp", "mcp:search_accounts", "missing"), ("mcp", "mcp:changes", "revoked"), ("mcp", "mcp:account", "ok")]


def test_mcp_answers_are_capped_and_say_so(keys, monkeypatch):
    from api import mcp_tools
    monkeypatch.setenv("XMETRICS_MCP_DSN", os.environ["XMETRICS_API_DSN"])
    monkeypatch.setenv("XMETRICS_MCP_KEY", keys["good"])
    monkeypatch.setattr(mcp_tools, "MAX_ROWS", 10)
    big = mcp_tools.search_accounts(limit=1000)
    assert big["returned"] == 10 and big["truncated"] is True and big["total"] == 95 and big["cap"] == 10
    small = mcp_tools.search_accounts(limit=3)
    assert small["returned"] == 3 and len(small["rows"]) == 3
    ch = mcp_tools.changes()
    assert ch["returned"] == 10 and ch["truncated"] is True
    assert all(r["flags"] for r in ch["rows"])                    # flagged rows come first


def test_mcp_validation_and_audit_answer_from_postgres(keys, monkeypatch):
    """The two tools the 10-02 move to Postgres dropped: back, through the same key check and log,
    answering from the reader login (pg/schema/008), in the SQLite-era shape."""
    from api import mcp_tools
    monkeypatch.setenv("XMETRICS_MCP_DSN", os.environ["XMETRICS_API_DSN"])
    monkeypatch.delenv("XMETRICS_MCP_KEY", raising=False)
    with pytest.raises(mcp_tools.Denied):
        mcp_tools.validation_summary()
    monkeypatch.setenv("XMETRICS_MCP_KEY", keys["good"])
    v = mcp_tools.validation_summary()
    assert v["rows"] == 95 and v["issues"] and all(k.endswith(("(error)", "(warn)")) for k in v["issues"])
    a = mcp_tools.agent_audit()
    assert a["attempts"] == sum(a["by_verdict"].values()) > 0 and set(a) == {"attempts", "by_verdict", "guardrail_fired", "classifiers"}
    assert [(r[1], r[2], r[3]) for r in log_rows()][-3:] == [
        ("mcp", "mcp:validation_summary", "missing"), ("mcp", "mcp:validation_summary", "ok"), ("mcp", "mcp:agent_audit", "ok")]
