"""The operations layer: an attempt is written down before it does anything, a failure names
its step and sends exactly one message, success sends none, a retry starts where the failure
was and reuses the run it had, and a process that dies is not left 'running' forever.

The pipeline runs against a Ledger and a Notifier; the first half of this file uses the
in-memory ones and fake steps, so it needs no server. The second half needs XMETRICS_PG_DSN
(like tests/test_pg.py) and checks the same contract against ops.pipeline_runs, the reader
role, and /health.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from ops import notify
from ops import pipeline
from ops.ledger import ABANDONED_AFTER, ABANDONED_DETAIL, INTERRUPTED_DETAIL, MemoryLedger
from ops.pipeline import Context, is_due, read_handles, recover_interrupted, retry, run_pipeline

ROOT = Path(__file__).resolve().parent.parent


class SpyNotifier:
    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self.ok = ok

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def fake_steps(calls: list[str], fail: str | None = None, run_id: str = "R1"):
    """Three steps that record their order; `fail` raises inside that step."""
    def collect(ctx):
        calls.append("collect"); ctx.run_id = run_id
        if fail == "collect":
            raise RuntimeError("X did not answer")
        return f"run {run_id}: measured 20"

    def load(ctx):
        calls.append("load")
        if fail == "load":
            raise ConnectionError("could not connect to server: Connection refused")
        return f"x.db: runs 1 for {ctx.run_id}"

    def diff(ctx):
        calls.append("diff")
        if fail == "diff":
            raise ValueError("no previous run")
        return "changes: 20 rows"
    return {"collect": collect, "load": load, "diff": diff}


def ctx() -> Context:
    return Context(db=Path("out/x.db"), out=Path("out"))


# ------------------------------------------------------------------- 1. happy path --
def test_ok_attempt_records_every_step_and_sends_nothing():
    led, spy, calls = MemoryLedger(), SpyNotifier(), []
    res = run_pipeline(ctx(), led, spy, steps=fake_steps(calls), trigger="scheduled", host="pc")
    assert res.ok and res.steps_done == ["collect", "load", "diff"] and calls == ["collect", "load", "diff"]
    row = led.get(res.attempt_id)
    assert row.status == "ok" and row.step is None and row.finished_at is not None
    assert row.run_id == "R1" and row.trigger == "scheduled" and row.host == "pc"
    assert spy.sent == []                      # success is silent


# ---------------------------------------------------------------------- 2. failure --
def test_failure_names_the_step_keeps_earlier_steps_and_alerts_once():
    led, spy, calls = MemoryLedger(), SpyNotifier(), []
    res = run_pipeline(ctx(), led, spy, steps=fake_steps(calls, fail="load"), host="pc")
    assert not res.ok and res.failed_step == "load" and res.steps_done == ["collect"]
    assert calls == ["collect", "load"]        # diff never ran
    row = led.get(res.attempt_id)
    assert row.status == "failed" and row.step == "load" and row.steps_done == ["collect"]
    assert "Connection refused" in row.detail
    assert row.run_id == "R1"                  # the collector's run is kept for the retry
    assert len(spy.sent) == 1 and row.alerted_at is not None
    msg = spy.sent[0]
    assert f"#{res.attempt_id}" in msg and "*load*" in msg and "Connection refused" in msg
    assert f"--retry {res.attempt_id}" in msg


def test_alert_that_could_not_be_sent_is_not_marked_sent():
    led, spy = MemoryLedger(), SpyNotifier(ok=False)
    res = run_pipeline(ctx(), led, spy, steps=fake_steps([], fail="diff"))
    assert not res.ok and not res.alerted and led.get(res.attempt_id).alerted_at is None


def test_rehearsal_flag_fails_inside_the_named_step():
    led, spy, calls = MemoryLedger(), SpyNotifier(), []
    c = ctx(); c.fail_at = "load"
    res = run_pipeline(c, led, spy, steps=fake_steps(calls))
    assert res.failed_step == "load" and calls == ["collect"] and "rehearsal" in res.detail
    assert len(spy.sent) == 1


# ------------------------------------------------------------------------ 3. retry --
def test_retry_starts_at_the_failed_step_with_the_same_run():
    led, spy, calls = MemoryLedger(), SpyNotifier(), []
    failed = run_pipeline(ctx(), led, spy, steps=fake_steps(calls, fail="load"))
    calls.clear()
    res = retry(failed.attempt_id, ctx(), led, spy, steps=fake_steps(calls))   # load works this time
    assert res.ok and calls == ["load", "diff"]                                # collect not repeated
    row = led.get(res.attempt_id)
    assert row.trigger == "retry" and row.retry_of == failed.attempt_id and row.run_id == "R1"
    assert led.get(failed.attempt_id).status == "failed"                       # history is not rewritten


def test_retry_of_a_failed_collect_collects_again_under_a_new_run():
    led, spy, calls = MemoryLedger(), SpyNotifier(), []
    failed = run_pipeline(ctx(), led, spy, steps=fake_steps(calls, fail="collect"))
    assert led.get(failed.attempt_id).run_id == "R1"     # collect had opened R1 before it died
    calls.clear()
    res = retry(failed.attempt_id, ctx(), led, spy, steps=fake_steps(calls, run_id="R2"))
    assert res.ok and calls == ["collect", "load", "diff"] and res.run_id == "R2"


def test_retry_refuses_what_is_not_a_failure():
    led, spy = MemoryLedger(), SpyNotifier()
    ok = run_pipeline(ctx(), led, spy, steps=fake_steps([]))
    with pytest.raises(ValueError, match="not failed"):
        retry(ok.attempt_id, ctx(), led, spy, steps=fake_steps([]))
    with pytest.raises(ValueError, match="no attempt"):
        retry(999, ctx(), led, spy, steps=fake_steps([]))


# --------------------------------------------------------------- 4. abandoned rows --
def test_a_running_row_older_than_the_limit_is_marked_failed_at_next_start():
    led = MemoryLedger()
    stuck = led.start("scheduled", "pc", None)
    led.rows[stuck].started_at -= ABANDONED_AFTER + timedelta(minutes=1)
    fresh = led.start("manual", "pc", None)                  # start() sweeps first
    assert led.get(stuck).status == "failed" and led.get(stuck).detail == ABANDONED_DETAIL
    assert led.get(fresh).status == "running"


# ------------------------------------- 4b. what 2026-10-04 taught (ops/README.md) --
def test_an_interrupted_attempt_is_closed_and_reported_once_at_the_next_start():
    """A shutdown kills the process mid-run: no finish, no message. The next start, holding the
    lock, closes it however young it is and sends one message naming it and its retry."""
    led, spy = MemoryLedger(), SpyNotifier()
    cut = led.start("scheduled", "pc", None); led.step(cut, "collect")
    dead = recover_interrupted(led, spy)
    assert [a.id for a in dead] == [cut]
    row = led.get(cut)
    assert row.status == "failed" and row.detail == INTERRUPTED_DETAIL and row.step == "collect"
    assert len(spy.sent) == 1 and f"#{cut}" in spy.sent[0] and f"--retry {cut}" in spy.sent[0]
    assert row.alerted_at is not None
    assert recover_interrupted(led, spy) == [] and len(spy.sent) == 1     # said once


def test_due_means_no_success_yet_or_the_last_one_is_old_enough():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 11, 13, 0, tzinfo=timezone.utc)
    assert is_due(None, now, 6)
    assert not is_due(now - timedelta(days=5, hours=23), now, 6)
    assert is_due(now - timedelta(days=6), now, 6)
    assert is_due(now - timedelta(days=14), now, 6)          # 9/28 -> 10/11: the missed week


def test_last_ok_run_counts_only_attempts_that_carried_a_run():
    led, spy = MemoryLedger(), SpyNotifier()
    assert led.last_ok_run() is None
    rehearsal = run_pipeline(ctx(), led, spy, steps=fake_steps([], run_id=None))
    assert rehearsal.ok and led.last_ok_run() is None              # finished, but no collector run
    real = run_pipeline(ctx(), led, spy, steps=fake_steps([]))
    assert led.last_ok_run() == led.get(real.attempt_id).started_at


def test_an_unreachable_database_still_sends_the_alert(monkeypatch):
    """The 10-04 failure: the server takes the connection and never answers. The pipeline must
    give up within the limit and alert without the ledger, which lives in that database."""
    import socket
    import time
    hole = socket.socket(); hole.bind(("127.0.0.1", 0)); hole.listen(8)    # accepts, never speaks
    port = hole.getsockname()[1]
    spy = SpyNotifier()
    monkeypatch.setattr(pipeline, "CONNECT_TIMEOUT", 2)
    monkeypatch.setattr(notify, "from_env", lambda: spy)
    t = time.monotonic()
    try:
        rc = pipeline.main(["--dsn", f"postgresql://x:y@127.0.0.1:{port}/x", "--trigger", "scheduled", "--if-due", "6"])
    finally:
        hole.close()
    assert rc == 1 and time.monotonic() - t < 15
    assert len(spy.sent) == 1 and "could not reach the database" in spy.sent[0] and "2 s" in spy.sent[0]


# ------------------------------------------------------------------ 5. the message --
def test_failure_message_has_step_first_error_line_and_retry_command():
    msg = notify.failure_message(7, "load", "psycopg.OperationalError: connection refused\n  at line 3", "pc-1",
                                 "python -m ops.pipeline --retry 7")
    assert "#7" in msg and "*load*" in msg and "pc-1" in msg
    assert "connection refused" in msg and "at line 3" not in msg   # first line only
    assert msg.endswith("`python -m ops.pipeline --retry 7`")


def test_slack_notifier_posts_json_and_reports_http_status(monkeypatch):
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["ct"] = req.full_url, req.data, req.get_header("Content-type")
        return Resp()
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert notify.SlackNotifier("https://hooks.slack.com/services/x").send("hello") is True
    assert seen["url"].startswith("https://hooks.slack.com/") and b'"text": "hello"' in seen["body"]
    assert seen["ct"] == "application/json"


def test_no_webhook_means_null_notifier(monkeypatch):
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    assert isinstance(notify.from_env(), notify.NullNotifier)
    assert notify.from_env().send("x") is False


def test_handles_file_reads_the_remeasure_list():
    handles = read_handles(ROOT / "docs" / "remeasure-20.csv")
    assert len(handles) == 20 and all(h == h.lower() and not h.startswith("@") for h in handles)


# ========================================================== against PostgreSQL ========
psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("XMETRICS_PG_DSN")
pg = pytest.mark.skipif(not DSN, reason="XMETRICS_PG_DSN not set")


@pytest.fixture(scope="module")
def owner():
    """Fresh schemas with the real data and a reader login, like tests/test_api.py."""
    from pg.load import load
    from pg.migrate import migrate
    from pg.roles import ensure_reader
    from tests.test_api import READER_PW, READER_USER, SQLITE_FILES, reader_dsn
    with psycopg.connect(DSN) as conn:
        conn.execute("DROP SCHEMA IF EXISTS raw, core, mart, pg, ops, auth CASCADE"); conn.commit()
        migrate(conn)
        for path in SQLITE_FILES:
            load(conn, path)
        ensure_reader(conn, READER_USER, READER_PW)
    os.environ["XMETRICS_API_DSN"] = reader_dsn(DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pg
def test_pg_ledger_round_trip_and_views(owner):
    from ops.ledger import PgLedger
    led, spy, calls = PgLedger(owner), SpyNotifier(), []
    failed = run_pipeline(ctx(), led, spy, steps=fake_steps(calls, fail="load"), trigger="scheduled", host="pc")
    ok = retry(failed.attempt_id, ctx(), led, spy, steps=fake_steps(calls))
    rows = owner.execute("SELECT id, status, trigger, step, steps_done, run_id, retry_of, alerted_at IS NOT NULL "
                         "FROM mart.v_pipeline_runs").fetchall()
    assert rows[0] == (ok.attempt_id, "ok", "retry", None, ["load", "diff"], "R1", failed.attempt_id, False)
    assert rows[1] == (failed.attempt_id, "failed", "scheduled", "load", ["collect"], "R1", None, True)
    latest = owner.execute("SELECT id, status FROM mart.v_pipeline_latest").fetchone()
    assert latest == (ok.attempt_id, "ok")


@pg
def test_pg_ledger_sweeps_abandoned_rows(owner):
    from ops.ledger import PgLedger
    led = PgLedger(owner)
    stuck = led.start("scheduled", "pc", None)
    owner.execute("UPDATE ops.pipeline_runs SET started_at = now() - interval '7 hours' WHERE id = %s", (stuck,))
    led.start("manual", "pc", None)
    assert led.get(stuck).status == "failed" and led.get(stuck).detail == ABANDONED_DETAIL


@pg
def test_reader_sees_the_ledger_but_cannot_write_it(owner):
    with psycopg.connect(os.environ["XMETRICS_API_DSN"], autocommit=True) as r:
        r.execute("SET default_transaction_read_only = off")
        assert r.execute("SELECT count(*) FROM mart.v_pipeline_runs").fetchone()[0] >= 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute("INSERT INTO ops.pipeline_runs (status, trigger) VALUES ('ok', 'manual')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute("UPDATE ops.pipeline_runs SET status = 'ok'")


@pg
def test_health_reports_the_last_attempt_and_fails_on_a_failed_one(owner, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from ops.ledger import PgLedger
    import api.main
    from api.main import app
    # The fixture data is real and only gets older. Without this the test passed on 09-28
    # and failed from 10-01 on: stale data alone makes ok false, so the assertion below
    # could no longer show that the failed attempt is what made it false.
    monkeypatch.setattr(api.main, "STALE_AFTER_DAYS", 36500)
    client, led, spy = TestClient(app), PgLedger(owner), SpyNotifier()
    good = run_pipeline(ctx(), led, spy, steps=fake_steps([]), trigger="scheduled")
    h = client.get("/health").json()
    assert h["last_pipeline"]["id"] == good.attempt_id and h["last_pipeline"]["status"] == "ok"
    bad = run_pipeline(ctx(), led, spy, steps=fake_steps([], fail="diff"), trigger="scheduled")
    h = client.get("/health").json()
    assert h["last_pipeline"]["id"] == bad.attempt_id and h["last_pipeline"]["status"] == "failed"
    assert h["last_pipeline"]["step"] == "diff" and h["ok"] is False and h["stale"] is False


# ------------------------------------------------- 6. the report comes from the database --
@pg
def test_report_from_sql_equals_python_over_the_merged_history(owner, tmp_path):
    """pg/report.py rebuilds the DiffResult from mart.changes_since; it must equal what
    xmetrics.diff computes in Python over a merged copy of every SQLite file — field for field,
    including the run counts and the incomplete lists."""
    from dataclasses import asdict
    from pg.report import changes_from_sql
    from xmetrics import diff as df
    from tests.test_pg import merged_store
    st = merged_store(tmp_path)
    for r in df.runs_with_measurements(st):
        sql, py = changes_from_sql(owner, r), df.compare(st, run_now=r)
        assert [asdict(c) for c in sql.changes] == [asdict(c) for c in py.changes], r
        assert (sql.run_prev, sql.prev_runs, sql.targets, sql.failed, sql.not_reached, sql.mode) == \
               (py.run_prev, py.prev_runs, py.targets, py.failed, py.not_reached, py.mode), r
        assert df.summary_line(sql) == df.summary_line(py)
    st.close()


@pg
@pytest.mark.skipif(not (ROOT / "out" / "x.db").exists(), reason="no out/x.db")
def test_diff_step_writes_the_sql_report_and_refuses_a_disagreement(owner, tmp_path):
    from ops.pipeline import step_diff
    from xmetrics import diff as df
    from xmetrics.store import Store
    st = Store(ROOT / "out" / "x.db")
    run = df.runs_with_measurements(st)[-1]
    st.close()
    c = Context(db=ROOT / "out" / "x.db", out=tmp_path, dsn=DSN, run_id=run)
    detail = step_diff(c)
    assert "SQL == Python on" in detail and (tmp_path / "changes.md").exists()
    import json
    body = json.loads((tmp_path / "changes.json").read_text(encoding="utf-8"))
    assert body["run_now"] == run and body["counts"]["total"] == len(body["changes"]) > 0
    # now make the database lie about one account both sides can see, and the step must not pass
    handle = owner.execute(
        "SELECT handle FROM mart.changes_since(%s) WHERE run_prev IN (SELECT run_id FROM raw.runs WHERE source_db = 'x.db') LIMIT 1",
        (run,)).fetchone()[0]
    owner.execute("UPDATE raw.measurements SET followers = followers + 1000 WHERE handle = %s AND run_id = %s", (handle, run))
    try:
        with pytest.raises(RuntimeError, match="disagree"):
            step_diff(c)
    finally:
        owner.execute("UPDATE raw.measurements SET followers = followers - 1000 WHERE handle = %s AND run_id = %s", (handle, run))


# ----------------------------------------- 7. the lock, the sweep and --if-due in SQL --
@pg
def test_pg_sweep_closes_running_rows_of_any_age(owner):
    from ops.ledger import PgLedger
    led, spy = PgLedger(owner), SpyNotifier()
    cut = led.start("scheduled", "pc", None); led.step(cut, "collect")      # seconds old, not hours
    dead = recover_interrupted(led, spy)
    assert cut in [a.id for a in dead]
    assert led.get(cut).status == "failed" and led.get(cut).detail == INTERRUPTED_DETAIL
    assert owner.execute("SELECT count(*) FROM ops.pipeline_runs WHERE status = 'running'").fetchone()[0] == 0
    assert led.get(cut).alerted_at is not None and len(spy.sent) == 1


@pg
def test_a_second_attempt_does_nothing_while_one_holds_the_lock(owner, monkeypatch):
    spy = SpyNotifier()
    monkeypatch.setattr(notify, "from_env", lambda: spy)
    before = owner.execute("SELECT count(*) FROM ops.pipeline_runs").fetchone()[0]
    with psycopg.connect(DSN, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (pipeline.LOCK_KEY,))
        assert pipeline.main(["--dsn", DSN, "--trigger", "scheduled"]) == 0
    assert owner.execute("SELECT count(*) FROM ops.pipeline_runs").fetchone()[0] == before and spy.sent == []


@pg
def test_if_due_skips_when_the_last_successful_run_is_recent(owner, monkeypatch, capsys):
    from ops.ledger import PgLedger
    spy = SpyNotifier()
    monkeypatch.setattr(notify, "from_env", lambda: spy)
    run_pipeline(ctx(), PgLedger(owner), spy, steps=fake_steps([]), trigger="scheduled")   # ok, with run R1, just now
    before = owner.execute("SELECT count(*) FROM ops.pipeline_runs").fetchone()[0]
    assert pipeline.main(["--dsn", DSN, "--trigger", "scheduled", "--if-due", "6"]) == 0
    assert "not due" in capsys.readouterr().out
    assert owner.execute("SELECT count(*) FROM ops.pipeline_runs").fetchone()[0] == before
    # the lock went with main()'s connection: the next attempt can take it
    assert owner.execute("SELECT pg_try_advisory_lock(%s)", (pipeline.LOCK_KEY,)).fetchone()[0]
    owner.execute("SELECT pg_advisory_unlock(%s)", (pipeline.LOCK_KEY,))
