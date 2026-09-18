"""What changed since the last run.

A single measurement answers "what is this account like?". Two measurements answer the
question a client actually pays a retainer for: "what moved since last week, and which
of these accounts should I look at again?"

Nothing here is estimated. Every delta is computed from two rows already in `measurements`
(the same rows validate.py recomputes), so a change is exactly as trustworthy as the two
numbers it came from. Thresholds only decide what gets *flagged*; every delta is reported.

    changes = compare(store)                 # latest two runs, all accounts
    text    = report(changes)                # changes.md
    payload = as_json(changes)               # changes.json (for Slack / n8n / a dashboard)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .store import Store


@dataclass(frozen=True)
class Thresholds:
    """What counts as 'worth a human's attention'. Relative where the base varies by
    orders of magnitude (followers), absolute in percentage points where the metric is
    already a rate. Defaults are deliberately conservative: a flag should mean something."""
    followers_pct: float = 5.0        # |Δfollowers| / previous followers, in %
    engagement_pp: float = 0.2        # |Δ engagement_rate| in percentage points
    engagement_rel_pct: float = 25.0  # ...and at least this much relative change
    views_rel_pct: float = 25.0       # |Δ views_to_followers| relative, in %
    silent_days: int = 14             # days_since_last_post crossing this = "went silent"


@dataclass
class Change:
    handle: str
    display: str
    kind: str                         # new | dropped | changed | unchanged
    flags: list[str] = field(default_factory=list)   # which thresholds fired
    followers_prev: int | None = None
    followers_now: int | None = None
    followers_delta: int | None = None
    followers_delta_pct: float | None = None
    engagement_prev: float | None = None
    engagement_now: float | None = None
    engagement_delta_pp: float | None = None
    views_prev: float | None = None
    views_now: float | None = None
    views_delta_pct: float | None = None
    tier_prev: str | None = None
    tier_now: str | None = None
    days_since_last_post_prev: int | None = None
    days_since_last_post_now: int | None = None
    posts_measured_prev: int | None = None
    posts_measured_now: int | None = None


@dataclass
class DiffResult:
    run_prev: str | None
    run_now: str | None
    changes: list[Change]

    @property
    def new(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "new"]

    @property
    def dropped(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "dropped"]

    @property
    def flagged(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "changed" and c.flags]

    @property
    def unchanged(self) -> list[Change]:
        return [c for c in self.changes if c.kind in ("changed", "unchanged") and not c.flags]


# --------------------------------------------------------------------------- queries --
def runs_with_measurements(store: Store) -> list[str]:
    """Run ids that actually produced measurements, oldest first.
    Ordered by the runs table's started_at, not by run_id string, so an imported legacy run
    with an older timestamp sorts where it belongs."""
    rows = store.conn.execute(
        "SELECT r.run_id FROM runs r WHERE EXISTS (SELECT 1 FROM measurements m WHERE m.run_id = r.run_id) "
        "ORDER BY r.started_at, r.run_id"
    ).fetchall()
    return [r["run_id"] for r in rows]


def measurements_for_run(store: Store, run_id: str) -> dict[str, sqlite3.Row]:
    rows = store.conn.execute(
        "SELECT a.display, a.status, m.* FROM measurements m JOIN accounts a ON a.handle = m.handle WHERE m.run_id = ?",
        (run_id,),
    ).fetchall()
    return {r["handle"]: r for r in rows}


# ------------------------------------------------------------------------- compare --
def _rel(delta: float, base: float) -> float | None:
    return round(delta / base * 100, 2) if base else None


def compare_rows(prev: sqlite3.Row | None, now: sqlite3.Row | None, th: Thresholds = Thresholds()) -> Change:
    handle = (now or prev)["handle"]
    display = (now or prev)["display"] or handle
    if prev is None:
        return Change(handle, display, "new", followers_now=now["followers"], engagement_now=now["engagement_rate"],
                      views_now=now["views_to_followers"], tier_now=now["tier"],
                      days_since_last_post_now=now["days_since_last_post"], posts_measured_now=now["posts_measured"])
    if now is None:
        return Change(handle, display, "dropped", followers_prev=prev["followers"], engagement_prev=prev["engagement_rate"],
                      views_prev=prev["views_to_followers"], tier_prev=prev["tier"],
                      days_since_last_post_prev=prev["days_since_last_post"], posts_measured_prev=prev["posts_measured"])

    c = Change(
        handle, display, "changed",
        followers_prev=prev["followers"], followers_now=now["followers"],
        followers_delta=now["followers"] - prev["followers"],
        followers_delta_pct=_rel(now["followers"] - prev["followers"], prev["followers"]),
        engagement_prev=prev["engagement_rate"], engagement_now=now["engagement_rate"],
        engagement_delta_pp=round(now["engagement_rate"] - prev["engagement_rate"], 4),
        views_prev=prev["views_to_followers"], views_now=now["views_to_followers"],
        views_delta_pct=_rel(now["views_to_followers"] - prev["views_to_followers"], prev["views_to_followers"]),
        tier_prev=prev["tier"], tier_now=now["tier"],
        days_since_last_post_prev=prev["days_since_last_post"], days_since_last_post_now=now["days_since_last_post"],
        posts_measured_prev=prev["posts_measured"], posts_measured_now=now["posts_measured"],
    )

    if c.followers_delta_pct is not None and abs(c.followers_delta_pct) >= th.followers_pct:
        c.flags.append("followers_up" if c.followers_delta > 0 else "followers_down")
    rel = _rel(c.engagement_delta_pp, prev["engagement_rate"])
    if abs(c.engagement_delta_pp) >= th.engagement_pp and (rel is None or abs(rel) >= th.engagement_rel_pct):
        c.flags.append("engagement_up" if c.engagement_delta_pp > 0 else "engagement_down")
    if c.views_delta_pct is not None and abs(c.views_delta_pct) >= th.views_rel_pct:
        c.flags.append("views_up" if c.views_delta_pct > 0 else "views_down")
    if c.tier_prev != c.tier_now:
        c.flags.append("tier_change")
    dp, dn = c.days_since_last_post_prev, c.days_since_last_post_now
    if dp is not None and dn is not None:
        if dp < th.silent_days <= dn:
            c.flags.append("went_silent")
        elif dn < th.silent_days <= dp:
            c.flags.append("active_again")

    if not c.flags and c.followers_delta == 0 and c.engagement_delta_pp == 0 and (c.views_delta_pct in (0, None)):
        c.kind = "unchanged"
    return c


def compare(store: Store, run_prev: str | None = None, run_now: str | None = None,
            th: Thresholds = Thresholds()) -> DiffResult:
    """Compare two runs. Defaults to the two most recent runs that have measurements.
    With fewer than two runs there is nothing to compare — the result says so instead of guessing."""
    runs = runs_with_measurements(store)
    if run_now is None:
        run_now = runs[-1] if runs else None
    if run_prev is None:
        earlier = [r for r in runs if r != run_now]
        run_prev = earlier[-1] if earlier else None
    if run_now is None or run_prev is None:
        return DiffResult(run_prev, run_now, [])
    if run_now not in runs or run_prev not in runs:
        raise ValueError(f"unknown run id(s): have {runs}")

    a = measurements_for_run(store, run_prev)
    b = measurements_for_run(store, run_now)
    changes = [compare_rows(a.get(h), b.get(h), th) for h in sorted(set(a) | set(b))]
    # most interesting first: dropped, new, then flagged by size of the follower move
    order = {"dropped": 0, "new": 1, "changed": 2, "unchanged": 3}
    changes.sort(key=lambda c: (order[c.kind], 0 if c.flags else 1, -abs(c.followers_delta_pct or 0), c.handle))
    return DiffResult(run_prev, run_now, changes)


# -------------------------------------------------------------------------- output --
def _fmt_pct(v: float | None, sign: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%" if sign else f"{v:.2f}%"


def summary_line(res: DiffResult) -> str:
    if res.run_now is None or res.run_prev is None:
        return "changes: nothing to compare yet (need two runs with measurements)"
    return (f"changes since {res.run_prev} → {res.run_now}: "
            f"{len(res.flagged)} flagged | {len(res.new)} new | {len(res.dropped)} dropped | "
            f"{len(res.unchanged)} unchanged of {len(res.changes)}")


def report(res: DiffResult) -> str:
    lines = [f"# What changed — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", summary_line(res), ""]
    if res.run_now is None or res.run_prev is None:
        return "\n".join(lines) + "\n"
    lines += [f"previous run: `{res.run_prev}`  ·  this run: `{res.run_now}`", ""]

    if res.flagged:
        lines += ["## Flagged (crossed a threshold)", "",
                  "| handle | flags | followers | engagement | views/followers | last post (days) |", "|---|---|---|---|---|---|"]
        for c in res.flagged:
            tier = f" ({c.tier_prev} → {c.tier_now})" if "tier_change" in c.flags else ""
            lines.append(
                f"| @{c.display} | {', '.join(c.flags)} | {c.followers_prev:,} → {c.followers_now:,} ({_fmt_pct(c.followers_delta_pct)}){tier} "
                f"| {c.engagement_prev:.2f}% → {c.engagement_now:.2f}% ({c.engagement_delta_pp:+.2f} pp) "
                f"| {c.views_prev:.1f}% → {c.views_now:.1f}% ({_fmt_pct(c.views_delta_pct)}) "
                f"| {c.days_since_last_post_prev} → {c.days_since_last_post_now} |")
        lines.append("")
    if res.new:
        lines += ["## New this run", ""] + [f"- @{c.display} — {c.followers_now:,} followers, ER {c.engagement_now:.2f}%, {c.tier_now}" for c in res.new] + [""]
    if res.dropped:
        lines += ["## Not measured this run (were in the previous one)", ""] + [
            f"- @{c.display} — last seen {c.followers_prev:,} followers, ER {c.engagement_prev:.2f}%" for c in res.dropped] + [""]
    if res.unchanged:
        lines += [f"## Within thresholds ({len(res.unchanged)})", "",
                  "| handle | followers | engagement | views/followers |", "|---|---|---|---|"]
        for c in res.unchanged:
            lines.append(f"| @{c.display} | {c.followers_prev:,} → {c.followers_now:,} ({_fmt_pct(c.followers_delta_pct)}) "
                         f"| {c.engagement_prev:.2f}% → {c.engagement_now:.2f}% | {c.views_prev:.1f}% → {c.views_now:.1f}% |")
        lines.append("")
    return "\n".join(lines) + "\n"


def as_json(res: DiffResult) -> dict:
    return {
        "run_prev": res.run_prev, "run_now": res.run_now,
        "summary": summary_line(res),
        "counts": {"flagged": len(res.flagged), "new": len(res.new), "dropped": len(res.dropped),
                   "unchanged": len(res.unchanged), "total": len(res.changes)},
        "changes": [asdict(c) for c in res.changes],
    }


def write_outputs(res: DiffResult, out_dir) -> dict[str, str]:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / "changes.md"
    js = out / "changes.json"
    md.write_text(report(res), encoding="utf-8")
    js.write_text(json.dumps(as_json(res), indent=2, ensure_ascii=False), encoding="utf-8")
    return {"md": str(md), "json": str(js)}
