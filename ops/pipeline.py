"""The weekly job, as one command: collect -> load -> diff, written down as it goes.

    python -m ops.pipeline                                  # all three steps; targets from --handles-file
    python -m ops.pipeline --trigger scheduled              # what the scheduler calls
    python -m ops.pipeline --from load --run 2026...Z-playwright   # the collector already ran: load + diff that run
    python -m ops.pipeline --retry 12                       # attempt #12 failed: redo it from the step that failed
    python -m ops.pipeline --fail-at load                   # rehearse a failure (records it, sends the alert)
    python -m ops.pipeline --trigger scheduled --if-due 6   # what the scheduler calls: run only if the last
                                                            # successful run is 6+ days old

Before anything else it must reach the database within CONNECT_TIMEOUT seconds; if it cannot,
it sends a Slack message that does not need the database, and exits 1. Then it takes the
pipeline lock (one attempt at a time). Holding the lock proves no other attempt is alive, so
any row still 'running' belongs to a process that died — shut down, closed, killed — and is
marked failed with one message. That is what happened on 2026-10-04 (ops/README.md): the
database container was not up, the connection waited with no limit, the PC was switched off
40 minutes later, and nothing anywhere said so.

Every attempt is a row in ops.pipeline_runs (pg/schema/006) from the moment it starts:
'running' while it works, then 'ok' or 'failed' with the step that failed and the error.
/health shows the newest row; the dashboard has a card for the last ten.

Failure sends exactly one Slack message (ops/notify.py) naming the step, the error and the
retry command. Success sends nothing. Exit code: 0 ok, 1 failed — so a scheduler sees it too.

Retry re-runs from the failed step with the same collector run_id, so a load that failed
half-way is loaded again (idempotent: pg/load.py upserts) and nothing before it is repeated.
If collect itself failed, the retry collects again — that is a new run_id, and the row says so.

What each step needs:
  collect  a browser profile logged in to X (xmetrics login / import-cookies) and Playwright.
           Runs only on the machine that has them — this is why the job is scheduled on a PC,
           not in the compose stack.
  load     XMETRICS_PG_DSN (the owner login: this is the one process that writes).
  diff     XMETRICS_PG_DSN too: the report is read from mart.changes_since (the whole history),
           written to out/changes.md + changes.json, then checked against the local Python diff.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import socket
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import notify
from .ledger import Ledger

log = logging.getLogger("ops.pipeline")

STEPS = ("collect", "load", "diff")
CONNECT_TIMEOUT = int(os.environ.get("XMETRICS_PG_CONNECT_TIMEOUT", "10"))   # seconds; libpq's minimum is 2
LOCK_KEY = 7_301_982_516          # pg_advisory_lock key for "the x-metrics pipeline"; any constant works


def connect(dsn: str, **kw):
    """Every connection the pipeline opens: never wait for the server without a limit."""
    import psycopg
    return psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT, **kw)


def is_due(last_ok, now, days: float) -> bool:
    """A run is due when there has been no successful one, or the last one is `days` old."""
    return last_ok is None or (now - last_ok).total_seconds() >= days * 86400


@dataclass
class Context:
    """Everything a step may need. Steps read and write `run_id`; nothing else changes."""
    db: Path
    out: Path
    profile: Path | None = None
    handles: list[str] = field(default_factory=list)
    everything: bool = False
    posts: int = 20
    dsn: str | None = None
    run_id: str | None = None
    attempt_id: int | None = None
    headless: bool = False            # headed: the only mode that has collected from X for real (09-23, 10-08)
    fail_at: str | None = None        # rehearsal: raise inside this step


StepFn = Callable[[Context], str]      # returns a one-line detail for the ledger


@dataclass
class Result:
    attempt_id: int
    status: str
    steps_done: list[str]
    failed_step: str | None = None
    detail: str = ""
    run_id: str | None = None
    alerted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --------------------------------------------------------------------------- steps --
def step_collect(ctx: Context) -> str:
    from xmetrics.collect import Collector
    from xmetrics.store import Store
    if ctx.profile is None:
        raise RuntimeError("collect needs --profile (a browser profile logged in to X)")
    st = Store(ctx.db)
    note = f"pipeline #{ctx.attempt_id}" if ctx.attempt_id else "pipeline"
    run_id = Collector(st, ctx.profile, headless=ctx.headless, posts_per_account=ctx.posts).run(
        ctx.handles or None, note=note, everything=ctx.everything)
    if not run_id:
        raise RuntimeError("collect measured nothing: no pending accounts and no handles given")
    ctx.run_id = run_id
    tg = st.run_targets(run_id)
    counts = {o: sum(1 for v in tg.values() if v == o) for o in ("measured", "missing", "error", "pending")}
    if counts["pending"]:
        raise RuntimeError(f"run {run_id} left {counts['pending']} account(s) unreached")
    return f"run {run_id}: measured {counts['measured']}, missing {counts['missing']}, error {counts['error']}"


def step_load(ctx: Context) -> str:
    from pg.load import load
    if not ctx.dsn:
        raise RuntimeError("load needs XMETRICS_PG_DSN (the owner login)")
    if not ctx.run_id:
        raise RuntimeError("load needs a run_id (collect first, or pass --run)")
    with connect(ctx.dsn) as conn:
        res = load(conn, ctx.db, run_id=ctx.run_id)
    return str(res)


def step_diff(ctx: Context) -> str:
    """Write out/changes.md and changes.json for this run from the database (pg/report.py):
    the report the dashboard shows. Then recompute it in Python over the local SQLite file
    and refuse to finish if the two disagree on any account both sides can see — the CI
    assertion (tests/test_pg.py), repeated on every scheduled run, on real data."""
    from pg.report import changes_from_sql
    from xmetrics import diff as df
    from xmetrics.store import Store
    if not ctx.run_id:
        raise RuntimeError("diff needs a run_id")
    if not ctx.dsn:
        raise RuntimeError("diff needs XMETRICS_PG_DSN")
    with connect(ctx.dsn) as conn:
        res = changes_from_sql(conn, ctx.run_id)
    df.write_outputs(res, ctx.out)

    st = Store(ctx.db)
    local_runs = set(df.runs_with_measurements(st))
    py = {c.handle: c for c in df.compare(st, run_now=ctx.run_id).changes} if ctx.run_id in local_runs else {}
    checked, disagreed = [], []
    for c in res.changes:
        p = py.get(c.handle)
        # comparable only when the Python side had the same baseline in its file
        if p is None or (c.run_prev and c.run_prev not in local_runs):
            continue
        checked.append(c.handle)
        if p != c:
            disagreed.append(c.handle)
    if disagreed:
        raise RuntimeError(f"SQL and Python change reports disagree for {len(disagreed)} account(s): "
                           + ", ".join("@" + h for h in disagreed[:5]))
    return df.summary_line(res) + f" · SQL == Python on {len(checked)} of {len(res.changes)} rows"


DEFAULT_STEPS: dict[str, StepFn] = {"collect": step_collect, "load": step_load, "diff": step_diff}


# ------------------------------------------------------------------------ pipeline --
def run_pipeline(ctx: Context, ledger: Ledger, notifier: notify.Notifier, *,
                 steps: dict[str, StepFn] | None = None, trigger: str = "manual",
                 start_from: str = "collect", retry_of: int | None = None,
                 host: str | None = None) -> Result:
    steps = steps or DEFAULT_STEPS
    if start_from not in STEPS:
        raise ValueError(f"start_from must be one of {STEPS}")
    host = host or socket.gethostname()
    attempt = ledger.start(trigger, host, retry_of)
    ctx.attempt_id = attempt
    if ctx.run_id:
        ledger.set_run_id(attempt, ctx.run_id)
    done: list[str] = []
    todo = STEPS[STEPS.index(start_from):]
    log.info("attempt #%d (%s): steps %s", attempt, trigger, ", ".join(todo))
    for name in todo:
        ledger.step(attempt, name)
        try:
            if ctx.fail_at == name:
                raise RuntimeError(f"rehearsal: --fail-at {name}")
            detail = steps[name](ctx)
        except Exception as e:  # noqa: BLE001 — any failure is the pipeline's failure; record it, then alert
            err = "".join(traceback.format_exception_only(type(e), e)).strip() or repr(e)
            if ctx.run_id:                      # a collect that died after opening its run: keep the id, it is evidence
                ledger.set_run_id(attempt, ctx.run_id)
            ledger.finish_failed(attempt, name, done, err)
            log.error("attempt #%d failed at %s: %s", attempt, name, err)
            msg = notify.failure_message(attempt, name, err, host, f"python -m ops.pipeline --retry {attempt}")
            sent = notifier.send(msg)
            if sent:
                ledger.alerted(attempt)
            return Result(attempt, "failed", done, failed_step=name, detail=err, run_id=ctx.run_id, alerted=sent)
        if ctx.run_id:
            ledger.set_run_id(attempt, ctx.run_id)
        done.append(name)
        log.info("attempt #%d %s: %s", attempt, name, detail)
    summary = f"{', '.join(done)} ok" + (f" · run {ctx.run_id}" if ctx.run_id else "")
    ledger.finish_ok(attempt, done, summary)
    return Result(attempt, "ok", done, detail=summary, run_id=ctx.run_id)


def retry(attempt_id: int, ctx: Context, ledger: Ledger, notifier: notify.Notifier, *,
          steps: dict[str, StepFn] | None = None, host: str | None = None) -> Result:
    """Redo a failed attempt from the step that failed, with the run_id it had."""
    prev = ledger.get(attempt_id)
    if prev is None:
        raise ValueError(f"no attempt #{attempt_id}")
    if prev.status != "failed":
        raise ValueError(f"attempt #{attempt_id} is {prev.status}, not failed — nothing to retry")
    if not prev.step:
        raise ValueError(f"attempt #{attempt_id} has no failed step recorded")
    ctx.run_id = prev.run_id if prev.step != "collect" else None
    if prev.step != "collect" and not ctx.run_id:
        raise ValueError(f"attempt #{attempt_id} failed at {prev.step} but recorded no run_id; use --from {prev.step} --run <id>")
    return run_pipeline(ctx, ledger, notifier, steps=steps, trigger="retry",
                        start_from=prev.step, retry_of=attempt_id, host=host)


def recover_interrupted(ledger: Ledger, notifier: notify.Notifier) -> list:
    """Close every attempt left 'running' by a process that died, and say so once. Call only
    while holding the pipeline lock: that is what makes 'running' mean 'dead'."""
    dead = ledger.sweep_interrupted()
    if dead:
        log.warning("closed %d interrupted attempt(s): %s", len(dead), ", ".join(f"#{a.id}" for a in dead))
        if notifier.send(notify.interrupted_message(dead)):
            for a in dead:
                ledger.alerted(a.id)
    return dead


# ----------------------------------------------------------------------------- cli --
def read_handles(path: Path) -> list[str]:
    """First column of a CSV (header 'handle'), or one handle per line."""
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return []
    if rows[0] and rows[0][0].strip().lower() == "handle":
        rows = rows[1:]
    return [r[0].strip().lstrip("@") for r in rows if r and r[0].strip() and not r[0].startswith("#")]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", default="out/x.db")
    p.add_argument("--out", default="out")
    p.add_argument("--profile", default=".xmetrics-profile")
    p.add_argument("--handles-file", default="docs/remeasure-20.csv", help="accounts to collect (CSV with a 'handle' column)")
    p.add_argument("--all", action="store_true", help="collect every tracked account instead of --handles-file")
    p.add_argument("--posts", type=int, default=20)
    # Headed by default. On 2026-10-08 the headless browser was answered with a download instead of
    # x.com/home on every try; the headed one collected 19 of 20. The alert's retry command has no
    # flags, so the default has to be the mode that works.
    p.add_argument("--headless", action="store_true", help="no browser window (not verified against X; see ops/README.md)")
    p.add_argument("--headed", action="store_true", help=argparse.SUPPRESS)   # the default now; kept so old commands still run
    p.add_argument("--trigger", choices=("scheduled", "manual"), default="manual")
    p.add_argument("--from", dest="start_from", choices=STEPS, default="collect", help="start at this step")
    p.add_argument("--run", help="collector run_id to load/diff when starting after collect")
    p.add_argument("--retry", type=int, metavar="ID", help="redo failed attempt ID from the step that failed")
    p.add_argument("--fail-at", choices=STEPS, help="rehearsal: fail inside this step (records + alerts)")
    p.add_argument("--dsn", help="owner DSN (default: $XMETRICS_PG_DSN)")
    p.add_argument("--if-due", type=float, metavar="DAYS",
                   help="do nothing unless the last successful run started DAYS or more ago (the scheduler's mode)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dsn = args.dsn or os.environ.get("XMETRICS_PG_DSN")
    if not dsn:
        sys.exit("set XMETRICS_PG_DSN (owner login) or pass --dsn")
    if args.start_from != "collect" and not args.run and not args.retry:
        sys.exit(f"--from {args.start_from} needs --run <run_id>")

    handles = [] if args.all else read_handles(Path(args.handles_file))
    ctx = Context(db=Path(args.db), out=Path(args.out), profile=Path(args.profile), handles=handles,
                  everything=args.all, posts=args.posts, dsn=dsn, run_id=args.run,
                  headless=args.headless and not args.headed, fail_at=args.fail_at)

    import psycopg
    from datetime import datetime, timezone
    from .ledger import PgLedger
    notifier, host = notify.from_env(), socket.gethostname()
    try:
        conn = connect(dsn, autocommit=True)
    except psycopg.OperationalError as e:
        # No database, so no ledger row to write. The message must not depend on it.
        err = str(e).strip() or repr(e)
        log.error("database unreachable: %s", err)
        sent = notifier.send(notify.unreachable_message(host, err, CONNECT_TIMEOUT))
        print("database unreachable — nothing ran" + (" (alert sent)" if sent else ""))
        return 1
    with conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
            print("another pipeline attempt holds the lock — nothing to do")
            return 0
        ledger = PgLedger(conn)
        recover_interrupted(ledger, notifier)
        if args.if_due is not None and not args.retry:
            last = ledger.last_ok_run()
            if not is_due(last, datetime.now(timezone.utc), args.if_due):
                print(f"not due: last successful run started {last:%Y-%m-%d %H:%M} UTC (runs every {args.if_due:g} days)")
                return 0
        if args.retry:
            res = retry(args.retry, ctx, ledger, notifier, host=host)
        else:
            res = run_pipeline(ctx, ledger, notifier, trigger=args.trigger, start_from=args.start_from, host=host)
    print(f"attempt #{res.attempt_id}: {res.status}"
          + (f" at {res.failed_step}: {res.detail}" if res.failed_step else f" — {res.detail}")
          + (" (alert sent)" if res.alerted else ""))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
