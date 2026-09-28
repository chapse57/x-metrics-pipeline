"""Where each pipeline attempt is written down: ops.pipeline_runs in PostgreSQL, or memory
for tests. The pipeline talks to a Ledger and nothing else, so the same code path is tested
with no database and run with one.

An attempt's life: start() -> step() per step -> finish_ok() | finish_failed() (-> alerted()).
A process that dies between start() and finish leaves a 'running' row; the next start()
calls sweep() and marks any 'running' row older than ABANDONED_AFTER as failed, because
a run that has not finished in that long has not finished.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

ABANDONED_AFTER = timedelta(hours=6)
ABANDONED_DETAIL = "no finish recorded — the process died or was killed before it could report"


@dataclass
class Attempt:
    id: int
    started_at: datetime
    status: str                      # running | ok | failed
    trigger: str                     # scheduled | manual | retry
    host: str | None = None
    step: str | None = None
    steps_done: list[str] = field(default_factory=list)
    run_id: str | None = None
    retry_of: int | None = None
    detail: str | None = None
    finished_at: datetime | None = None
    alerted_at: datetime | None = None


class Ledger(Protocol):
    def start(self, trigger: str, host: str | None, retry_of: int | None) -> int: ...
    def step(self, attempt_id: int, name: str) -> None: ...
    def set_run_id(self, attempt_id: int, run_id: str) -> None: ...
    def finish_ok(self, attempt_id: int, steps_done: list[str], detail: str) -> None: ...
    def finish_failed(self, attempt_id: int, step: str, steps_done: list[str], detail: str) -> None: ...
    def alerted(self, attempt_id: int) -> None: ...
    def get(self, attempt_id: int) -> Attempt | None: ...
    def sweep(self, now: datetime | None = None) -> list[int]: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- memory --
class MemoryLedger:
    """The ledger tests use. Same contract, no server."""
    def __init__(self):
        self.rows: dict[int, Attempt] = {}

    def start(self, trigger, host, retry_of):
        self.sweep()
        i = len(self.rows) + 1
        self.rows[i] = Attempt(i, _now(), "running", trigger, host=host, retry_of=retry_of)
        return i

    def step(self, attempt_id, name):
        self.rows[attempt_id].step = name

    def set_run_id(self, attempt_id, run_id):
        self.rows[attempt_id].run_id = run_id

    def finish_ok(self, attempt_id, steps_done, detail):
        a = self.rows[attempt_id]
        a.status, a.step, a.steps_done, a.detail, a.finished_at = "ok", None, list(steps_done), detail, _now()

    def finish_failed(self, attempt_id, step, steps_done, detail):
        a = self.rows[attempt_id]
        a.status, a.step, a.steps_done, a.detail, a.finished_at = "failed", step, list(steps_done), detail, _now()

    def alerted(self, attempt_id):
        self.rows[attempt_id].alerted_at = _now()

    def get(self, attempt_id):
        return self.rows.get(attempt_id)

    def sweep(self, now=None):
        now = now or _now()
        swept = []
        for a in self.rows.values():
            if a.status == "running" and now - a.started_at > ABANDONED_AFTER:
                a.status, a.detail, a.finished_at = "failed", ABANDONED_DETAIL, now
                swept.append(a.id)
        return swept


# ------------------------------------------------------------------------- postgres --
class PgLedger:
    """ops.pipeline_runs, written as the database owner (the pipeline loads data; it is the
    one process that is allowed to write). Each call is its own short transaction, so a row
    is visible to /health the moment it is written — including 'running'."""
    def __init__(self, conn):
        self.conn = conn

    def start(self, trigger, host, retry_of):
        self.sweep()
        with self.conn.transaction():
            row = self.conn.execute(
                "INSERT INTO ops.pipeline_runs (status, trigger, host, retry_of) VALUES ('running', %s, %s, %s) RETURNING id",
                (trigger, host, retry_of)).fetchone()
        return row[0]

    def step(self, attempt_id, name):
        with self.conn.transaction():
            self.conn.execute("UPDATE ops.pipeline_runs SET step = %s WHERE id = %s", (name, attempt_id))

    def set_run_id(self, attempt_id, run_id):
        with self.conn.transaction():
            self.conn.execute("UPDATE ops.pipeline_runs SET run_id = %s WHERE id = %s", (run_id, attempt_id))

    def finish_ok(self, attempt_id, steps_done, detail):
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE ops.pipeline_runs SET status = 'ok', step = NULL, steps_done = %s, detail = %s, finished_at = now() WHERE id = %s",
                (list(steps_done), detail, attempt_id))

    def finish_failed(self, attempt_id, step, steps_done, detail):
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE ops.pipeline_runs SET status = 'failed', step = %s, steps_done = %s, detail = %s, finished_at = now() WHERE id = %s",
                (step, list(steps_done), detail, attempt_id))

    def alerted(self, attempt_id):
        with self.conn.transaction():
            self.conn.execute("UPDATE ops.pipeline_runs SET alerted_at = now() WHERE id = %s", (attempt_id,))

    def get(self, attempt_id):
        row = self.conn.execute(
            "SELECT id, started_at, status, trigger, host, step, steps_done, run_id, retry_of, detail, finished_at, alerted_at "
            "FROM ops.pipeline_runs WHERE id = %s", (attempt_id,)).fetchone()
        return Attempt(*row[:6], list(row[6] or []), *row[7:]) if row else None

    def sweep(self, now=None):
        with self.conn.transaction():
            rows = self.conn.execute(
                "UPDATE ops.pipeline_runs SET status = 'failed', detail = %s, finished_at = %s "
                "WHERE status = 'running' AND started_at < %s - %s::interval RETURNING id",
                (ABANDONED_DETAIL, now or _now(), now or _now(), f"{int(ABANDONED_AFTER.total_seconds())} seconds")).fetchall()
        return [r[0] for r in rows]
