"""What changed since the last measurement.

A single measurement answers "what is this account like?". Two measurements answer the
question a client actually pays a retainer for: "what moved since last week, and which
of these accounts should I look at again?"

Nothing here is estimated. Every delta is computed from two rows already in `measurements`
(the same rows validate.py recomputes), so a change is exactly as trustworthy as the two
numbers it came from. Thresholds only decide what gets *flagged*; every delta is reported.

The rule, in one sentence: **each account is compared with its own previous measurement, and
an account counts as "dropped" only when a run that was meant to cover it came back without it.**

Why not "this run vs the previous run"? Runs do not all cover the same accounts. A weekly run
may re-measure 20 of 95; a first live run may be 3. Comparing two runs as whole sets would then
call 75 accounts "dropped" that were simply not tried, and call an account "new" that has a
baseline two runs back. So:

- `compare(store)` / `compare(store, run_now=r)` — baseline mode. For every account measured in
  the run, the previous value is that account's latest earlier measurement, whichever run it
  came from. `new` = no earlier measurement at all. `dropped` = only if the run's scope is
  'full' (it tried every tracked account — see store.start_run): tracked accounts with an
  earlier measurement that this run did not return.
- `compare(store, run_prev=a, run_now=b)` — pair mode, both runs named explicitly: the two
  runs as whole sets, absence on either side reported as new/dropped. For "show me exactly
  these two".

    changes = compare(store)                 # latest run vs each account's previous measurement
    text    = report(changes)                # changes.md
    payload = as_json(changes)               # changes.json (for Slack / n8n / a dashboard)

mart.v_changes in PostgreSQL (pg/schema) implements the same rule in SQL, and tests/test_pg.py
asserts both sides return identical rows for every run.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

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
    run_prev: str | None = None       # the run the previous value came from (per account, in baseline mode)
    run_now: str | None = None


@dataclass
class DiffResult:
    run_prev: str | None              # pair mode: the named previous run.
                                      # baseline mode: the one run every baseline came from, else None
    run_now: str | None
    changes: list[Change]
    prev_runs: list[str] = field(default_factory=list)   # every run a baseline was taken from (sorted)
    scope: str | None = None          # run_now's scope: 'full' | 'partial' (None when nothing to compare)
    mode: str = "baseline"            # 'baseline' | 'pair'

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


def baseline_for_run(store: Store, run_id: str) -> dict[str, sqlite3.Row]:
    """Each tracked account's latest measurement from any run that started before `run_id`
    (ties on started_at broken by run_id, the same order runs_with_measurements uses).
    Includes accounts not in `run_id` — the caller decides whether their absence means anything."""
    rows = store.conn.execute(
        """
        WITH now AS (SELECT started_at, run_id FROM runs WHERE run_id = ?),
             earlier AS (
               SELECT m.*, r.started_at AS run_started_at,
                      ROW_NUMBER() OVER (PARTITION BY m.handle ORDER BY r.started_at DESC, r.run_id DESC) AS rn
               FROM measurements m JOIN runs r ON r.run_id = m.run_id, now
               WHERE (r.started_at, r.run_id) < (now.started_at, now.run_id)
             )
        SELECT a.display, a.status, e.*
        FROM earlier e JOIN accounts a ON a.handle = e.handle
        WHERE e.rn = 1 AND a.status != 'screened_out'
        """,
        (run_id,),
    ).fetchall()
    return {r["handle"]: r for r in rows}


# ------------------------------------------------------------------------- compare --
def _round(x: float, places: int) -> float:
    """Round the way PostgreSQL's ``round(x::numeric, n)`` does: half away from zero, on the
    shortest decimal form of the float. Python's built-in ``round`` is half-to-even on the binary
    value (``round(0.125, 2) == 0.12``; Postgres says 0.13). The SQL view ``mart.v_changes``
    recomputes every delta below, and the two are asserted equal — so both sides must round by
    the same rule. Postgres's rule was chosen because the dashboard reads the SQL side: a client
    checking a number sees the SQL answer, and Python must agree with it, not the other way round."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def _rel(delta: float, base: float) -> float | None:
    return _round(delta / base * 100, 2) if base else None


def compare_rows(prev: sqlite3.Row | None, now: sqlite3.Row | None, th: Thresholds = Thresholds()) -> Change:
    handle = (now or prev)["handle"]
    display = (now or prev)["display"] or handle
    if prev is None:
        return Change(handle, display, "new", followers_now=now["followers"], engagement_now=now["engagement_rate"],
                      views_now=now["views_to_followers"], tier_now=now["tier"],
                      days_since_last_post_now=now["days_since_last_post"], posts_measured_now=now["posts_measured"],
                      run_now=now["run_id"])
    if now is None:
        return Change(handle, display, "dropped", followers_prev=prev["followers"], engagement_prev=prev["engagement_rate"],
                      views_prev=prev["views_to_followers"], tier_prev=prev["tier"],
                      days_since_last_post_prev=prev["days_since_last_post"], posts_measured_prev=prev["posts_measured"],
                      run_prev=prev["run_id"])

    c = Change(
        handle, display, "changed", run_prev=prev["run_id"], run_now=now["run_id"],
        followers_prev=prev["followers"], followers_now=now["followers"],
        followers_delta=now["followers"] - prev["followers"],
        followers_delta_pct=_rel(now["followers"] - prev["followers"], prev["followers"]),
        engagement_prev=prev["engagement_rate"], engagement_now=now["engagement_rate"],
        engagement_delta_pp=_round(now["engagement_rate"] - prev["engagement_rate"], 4),
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


def _sort(changes: list[Change]) -> None:
    # most interesting first: dropped, new, then flagged by size of the follower move
    order = {"dropped": 0, "new": 1, "changed": 2, "unchanged": 3}
    changes.sort(key=lambda c: (order[c.kind], 0 if c.flags else 1, -abs(c.followers_delta_pct or 0), c.handle))


def compare(store: Store, run_prev: str | None = None, run_now: str | None = None,
            th: Thresholds = Thresholds()) -> DiffResult:
    """What changed, per the rule in the module docstring.

    No run_prev: baseline mode — run_now (default: the latest run with measurements) against
    each account's own previous measurement. run_prev given: pair mode — exactly those two runs.
    With fewer than two runs there is nothing to compare — the result says so instead of guessing."""
    runs = runs_with_measurements(store)
    if run_now is None:
        run_now = runs[-1] if runs else None
    if run_now is None or len(runs) < 2:
        return DiffResult(run_prev, run_now, [], mode="pair" if run_prev else "baseline")
    if run_now not in runs or (run_prev is not None and run_prev not in runs):
        raise ValueError(f"unknown run id(s): have {runs}")

    now = measurements_for_run(store, run_now)
    scope = store.run_scope(run_now)

    if run_prev is not None:  # pair mode: the two runs as whole sets
        prev = measurements_for_run(store, run_prev)
        changes = [compare_rows(prev.get(h), now.get(h), th) for h in sorted(set(prev) | set(now))]
        _sort(changes)
        return DiffResult(run_prev, run_now, changes, prev_runs=[run_prev], scope=scope, mode="pair")

    # baseline mode
    base = baseline_for_run(store, run_now)                # empty for the first run: everything is new
    changes = [compare_rows(base.get(h), now[h], th) for h in sorted(now)]
    if scope == "full":                                      # absence is a signal only when we tried
        changes += [compare_rows(base[h], None, th) for h in sorted(set(base) - set(now))]
    _sort(changes)
    used = sorted({base[h]["run_id"] for h in base if h in now or scope == "full"})
    return DiffResult(used[0] if len(used) == 1 else None, run_now, changes, prev_runs=used, scope=scope)


# -------------------------------------------------------------------------- output --
def _fmt_pct(v: float | None, sign: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}%" if sign else f"{v:.2f}%"


def _comparable(res: DiffResult) -> bool:
    return res.run_now is not None and bool(res.prev_runs or res.changes)


def _since(res: DiffResult) -> str:
    if res.run_prev is not None:
        return f"since {res.run_prev}"
    if not res.prev_runs:
        return "since nothing (every account is new)"
    return f"since each account's previous measurement ({res.prev_runs[0]} … {res.prev_runs[-1]})"


def summary_line(res: DiffResult) -> str:
    if not _comparable(res):
        return "changes: nothing to compare yet (need two runs with measurements)"
    return (f"changes {_since(res)} → {res.run_now} [{res.scope}]: "
            f"{len(res.flagged)} flagged | {len(res.new)} new | {len(res.dropped)} dropped | "
            f"{len(res.unchanged)} unchanged of {len(res.changes)}")


def report(res: DiffResult) -> str:
    lines = [f"# What changed — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", summary_line(res), ""]
    if not _comparable(res):
        return "\n".join(lines) + "\n"
    if res.run_prev is not None:
        lines += [f"previous run: `{res.run_prev}`  ·  this run: `{res.run_now}`"]
    else:
        lines += [f"this run: `{res.run_now}`  ·  each account vs its previous measurement, taken from "
                  + ", ".join(f"`{r}`" for r in res.prev_runs)]
    lines += [f"scope: {res.scope} — " + ("every tracked account was tried; accounts that came back empty are listed as not measured"
                                          if res.scope == "full" else
                                          "a named subset was tried; accounts not in this run are not listed"), ""]

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
        lines += ["## Not measured this run (were tried, came back empty)", ""] + [
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
        "prev_runs": res.prev_runs, "scope": res.scope, "mode": res.mode,
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
