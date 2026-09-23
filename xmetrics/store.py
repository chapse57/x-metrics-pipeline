"""SQLite store. One file, six tables, idempotent writes.

accounts      one row per handle (identity + bio + contact)
runs          one row per collection run (for resume + audit)
run_targets   one row per (run, account) the run set out to measure, with what became of it:
              pending (never reached) | measured | missing (account gone/suspended) | error
              (we got there and could not read it). This is what lets a change report say
              "dropped" only about accounts we actually went looking for.
measurements  one row per (account, run): the medians and rates that go to the client
posts         raw per-post counts behind each measurement (re-computable evidence)
agent_audit   every LLM classification attempt, with which guardrail passed/failed
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .metrics import Post, Summary
from .parse import normalize_handle

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  handle       TEXT PRIMARY KEY,           -- normalized, no '@', lowercase
  display      TEXT,                       -- handle as shown (case preserved)
  bio          TEXT,
  bio_url      TEXT,
  dm_open      INTEGER,                    -- 1/0/NULL(unknown)
  niche        TEXT,                       -- final label (after guardrails / review)
  niche_source TEXT,                       -- 'agent' | 'rule' | 'human' | 'legacy'
  fit_note     TEXT,
  hook_link    TEXT,
  hook_note    TEXT,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | measured | screened_out | error
  screen_reason TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  source     TEXT NOT NULL,                -- 'playwright' | 'legacy-import'
  note       TEXT
);
CREATE TABLE IF NOT EXISTS run_targets (
  run_id     TEXT NOT NULL REFERENCES runs(run_id),
  handle     TEXT NOT NULL,
  outcome    TEXT NOT NULL DEFAULT 'pending',  -- pending | measured | missing | error
  detail     TEXT,                             -- error message, or why the account is missing
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, handle)
);
CREATE TABLE IF NOT EXISTS measurements (
  handle            TEXT NOT NULL REFERENCES accounts(handle),
  run_id            TEXT NOT NULL REFERENCES runs(run_id),
  measured_at       TEXT NOT NULL,
  followers         INTEGER NOT NULL,
  posts_measured    INTEGER NOT NULL,
  range_start       TEXT, range_end TEXT,
  median_likes      REAL, median_replies REAL, median_reposts REAL, median_views REAL,
  engagement_rate   REAL NOT NULL,
  views_to_followers REAL NOT NULL,
  tier              TEXT NOT NULL,
  days_since_last_post INTEGER,
  PRIMARY KEY (handle, run_id)
);
CREATE TABLE IF NOT EXISTS posts (
  handle     TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  status_id  TEXT NOT NULL,
  posted_at  TEXT NOT NULL,
  replies INTEGER, reposts INTEGER, likes INTEGER, bookmarks INTEGER, views INTEGER,
  is_repost INTEGER, is_pinned INTEGER, is_reply INTEGER,
  raw_label  TEXT,                         -- the exact aria-label we parsed (evidence)
  PRIMARY KEY (handle, run_id, status_id)
);
CREATE TABLE IF NOT EXISTS agent_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  handle      TEXT NOT NULL,
  at          TEXT NOT NULL,
  classifier  TEXT NOT NULL,               -- 'claude:<model>' | 'rule'
  raw_output  TEXT,                        -- untouched model output
  parsed      TEXT,                        -- JSON after parsing (or NULL)
  verdict     TEXT NOT NULL,               -- accepted | review | rejected
  failed_checks TEXT                       -- JSON list of guardrail names that failed
);
CREATE INDEX IF NOT EXISTS idx_meas_handle ON measurements(handle);
CREATE INDEX IF NOT EXISTS idx_posts_handle ON posts(handle, run_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    BACKFILL_NOTE = "backfilled: runs before run_targets existed recorded only what they measured"

    def _migrate(self) -> None:
        """Upgrades for files written by an older version. Each step is guarded, so opening an
        old file upgrades it once and opening a new one does nothing."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "scope" in cols:
            # runs.scope (a one-word summary: full/partial) was replaced by run_targets, one row per
            # account tried. Two sources of truth is how the 92-dropped bug got hand-patched.
            self.conn.execute("ALTER TABLE runs DROP COLUMN scope")
        # Runs from before run_targets existed did not record what they set out to measure, only
        # what they measured. The honest reconstruction is targets = measured, all 'measured':
        # nothing is called missing or unreached for a run we cannot know that about.
        self.conn.execute(
            """INSERT OR IGNORE INTO run_targets (run_id, handle, outcome, detail, updated_at)
               SELECT m.run_id, m.handle, 'measured', ?, m.measured_at
               FROM measurements m
               WHERE NOT EXISTS (SELECT 1 FROM run_targets t WHERE t.run_id = m.run_id)""",
            (self.BACKFILL_NOTE,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- accounts -------------------------------------------------------
    def upsert_account(self, handle: str, **fields) -> str:
        h = normalize_handle(handle)
        now = utcnow()
        explicit_display = "display" in fields
        fields.setdefault("display", handle.strip().lstrip("@"))
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        # display keeps the case the account was first registered with (e.g. 'realFatCat1'),
        # unless a caller passes one explicitly; a lowercase normalized handle must not overwrite it
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "display" or explicit_display)
        if not updates:
            updates = "updated_at=excluded.updated_at"
        with self.tx() as c:
            c.execute(
                f"INSERT INTO accounts (handle, {cols}, created_at, updated_at) VALUES (?, {placeholders}, ?, ?) "
                f"ON CONFLICT(handle) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
                (h, *fields.values(), now, now),
            )
        return h

    def get_account(self, handle: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM accounts WHERE handle=?", (normalize_handle(handle),)).fetchone()

    def pending_handles(self) -> list[str]:
        """Accounts not yet measured — this is what makes a run resumable."""
        return [r["handle"] for r in self.conn.execute("SELECT handle FROM accounts WHERE status='pending' ORDER BY created_at")]

    def active_handles(self) -> list[str]:
        """Every account we still track: anything not screened out (what `collect --all` tries)."""
        return [r["handle"] for r in self.conn.execute(
            "SELECT handle FROM accounts WHERE status != 'screened_out' ORDER BY created_at")]

    def set_status(self, handle: str, status: str, reason: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE accounts SET status=?, screen_reason=?, updated_at=? WHERE handle=?",
                      (status, reason, utcnow(), normalize_handle(handle)))

    # ---- runs -----------------------------------------------------------
    OUTCOMES = ("pending", "measured", "missing", "error")

    def start_run(self, source: str, note: str | None = None, targets: Iterable[str] = ()) -> str:
        """Open a run and record what it is about to try. Every target starts 'pending'; the
        collector (or save_measurement) moves it to measured / missing / error as it goes, so a
        run that dies half-way leaves the truth behind: which accounts it never reached."""
        targets = [normalize_handle(h) for h in targets]
        base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + source
        run_id, n = base, 1
        with self.tx() as c:
            # run ids have second resolution; two runs started inside the same second must not
            # silently share one id (INSERT OR IGNORE used to do exactly that)
            while c.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                n += 1
                run_id = f"{base}-{n}"
            now = utcnow()
            c.execute("INSERT INTO runs (run_id, started_at, source, note) VALUES (?,?,?,?)",
                      (run_id, now, source, note))
            c.executemany("INSERT OR IGNORE INTO run_targets (run_id, handle, outcome, updated_at) VALUES (?,?,'pending',?)",
                          [(run_id, h, now) for h in targets])
        return run_id

    def mark_target(self, run_id: str, handle: str, outcome: str, detail: str | None = None) -> None:
        """What became of one account in one run. Registers the target if the run did not
        declare it up front (an explicitly named handle added mid-way)."""
        if outcome not in self.OUTCOMES:
            raise ValueError(f"outcome must be one of {self.OUTCOMES}, got {outcome!r}")
        with self.tx() as c:
            c.execute("INSERT INTO run_targets (run_id, handle, outcome, detail, updated_at) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(run_id, handle) DO UPDATE SET outcome=excluded.outcome, detail=excluded.detail, "
                      "updated_at=excluded.updated_at",
                      (run_id, normalize_handle(handle), outcome, detail, utcnow()))

    def run_targets(self, run_id: str) -> dict[str, str]:
        """handle -> outcome for one run."""
        return {r["handle"]: r["outcome"] for r in self.conn.execute(
            "SELECT handle, outcome FROM run_targets WHERE run_id=? ORDER BY handle", (run_id,))}

    def finish_run(self, run_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (utcnow(), run_id))

    # ---- measurements ---------------------------------------------------
    def save_measurement(self, handle: str, run_id: str, s: Summary, posts: list[Post] | None = None,
                         raw_labels: dict[str, str] | None = None) -> None:
        h = normalize_handle(handle)
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, run_id, utcnow(), s.followers, s.posts_measured, s.range_start, s.range_end,
                 s.median_likes, s.median_replies, s.median_reposts, s.median_views,
                 s.engagement_rate, s.views_to_followers, s.tier, s.days_since_last_post),
            )
            c.execute("INSERT INTO run_targets (run_id, handle, outcome, updated_at) VALUES (?,?,'measured',?) "
                      "ON CONFLICT(run_id, handle) DO UPDATE SET outcome='measured', detail=NULL, updated_at=excluded.updated_at",
                      (run_id, h, utcnow()))
            for p in posts or []:
                c.execute(
                    "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (h, run_id, p.status_id, p.posted_at.isoformat(), p.counts.replies, p.counts.reposts,
                     p.counts.likes, p.counts.bookmarks, p.counts.views, int(p.is_repost), int(p.is_pinned),
                     int(p.is_reply), (raw_labels or {}).get(p.status_id)),
                )
            c.execute("UPDATE accounts SET status='measured', updated_at=? WHERE handle=?", (utcnow(), h))

    def latest_measurements(self) -> list[sqlite3.Row]:
        """One row per account: the most recent measurement joined with account fields."""
        return self.conn.execute(
            """
            SELECT a.*, m.* FROM accounts a
            JOIN measurements m ON m.handle = a.handle
            WHERE m.measured_at = (SELECT MAX(measured_at) FROM measurements WHERE handle = a.handle)
              AND a.status = 'measured'
            ORDER BY m.engagement_rate DESC
            """
        ).fetchall()

    def posts_for(self, handle: str, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts WHERE handle=? AND run_id=? ORDER BY posted_at DESC",
                                 (normalize_handle(handle), run_id)).fetchall()

    # ---- agent audit ----------------------------------------------------
    def log_agent(self, handle: str, classifier: str, raw_output: str | None, parsed: dict | None,
                  verdict: str, failed_checks: list[str]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO agent_audit (handle, at, classifier, raw_output, parsed, verdict, failed_checks) VALUES (?,?,?,?,?,?,?)",
                      (normalize_handle(handle), utcnow(), classifier, raw_output,
                       json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
                       verdict, json.dumps(failed_checks)))

    def agent_audit_summary(self) -> dict:
        """Verdict counts plus how often each guardrail fired — the README's 'what the agent got wrong' table."""
        by_verdict = {r["verdict"]: r["n"] for r in self.conn.execute("SELECT verdict, COUNT(*) n FROM agent_audit GROUP BY verdict")}
        by_check: dict[str, int] = {}
        classifiers: dict[str, int] = {}
        for r in self.conn.execute("SELECT classifier, failed_checks FROM agent_audit"):
            classifiers[r["classifier"]] = classifiers.get(r["classifier"], 0) + 1
            for c in json.loads(r["failed_checks"] or "[]"):
                by_check[c] = by_check.get(c, 0) + 1
        return {"attempts": sum(by_verdict.values()), "by_verdict": by_verdict, "guardrail_fired": by_check, "classifiers": classifiers}
