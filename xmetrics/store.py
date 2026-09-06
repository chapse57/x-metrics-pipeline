"""SQLite store. One file, four tables, idempotent writes.

accounts      one row per handle (identity + bio + contact)
runs          one row per collection run (for resume + audit)
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
from typing import Iterator

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

    def set_status(self, handle: str, status: str, reason: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE accounts SET status=?, screen_reason=?, updated_at=? WHERE handle=?",
                      (status, reason, utcnow(), normalize_handle(handle)))

    # ---- runs -----------------------------------------------------------
    def start_run(self, source: str, note: str | None = None) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + source
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO runs (run_id, started_at, source, note) VALUES (?,?,?,?)",
                      (run_id, utcnow(), source, note))
        return run_id

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
