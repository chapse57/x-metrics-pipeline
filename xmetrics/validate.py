"""Checks that run before anything is exported. They are the same checks a human
did by hand on 2026-09-05 (and caught two median errors doing it) — now they run
every time, on every row, and they fail loudly.

Each check returns Issues; `severity` decides whether export is blocked:
  error  -> row is excluded from the deliverable, listed in the validation report
  warn   -> row is delivered, flagged in the report
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from .metrics import rates_from_medians, tier_for
from .parse import normalize_handle
from .store import Store


@dataclass(frozen=True)
class Issue:
    handle: str
    check: str
    severity: str  # 'error' | 'warn'
    detail: str


DEFAULT_RULES = {
    "min_posts": 10,            # fewer measured posts = median not trustworthy
    "max_days_since_post": 21,  # client asked for "active" accounts
    "min_followers": 1000,
    "require_contact": True,    # DM open or a bio URL
}


def _tol(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def check_recompute(store: Store, tol_rate: float = 0.006) -> list[Issue]:
    """Recompute every rate from the stored medians / posts and compare with what is saved.
    A rounding slip here is exactly the class of error the 09-05 manual pass caught."""
    issues: list[Issue] = []
    for r in store.latest_measurements():
        er, vr = rates_from_medians(r["followers"], r["median_likes"], r["median_replies"], r["median_reposts"], r["median_views"])
        posts = store.posts_for(r["handle"], r["run_id"])
        if posts:  # pipeline-collected: recompute medians from raw posts too
            own = [p for p in posts if not p["is_repost"] and not p["is_pinned"]]
            if len(own) != r["posts_measured"]:
                issues.append(Issue(r["handle"], "recompute.posts_measured", "error", f"stored {r['posts_measured']} vs raw {len(own)}"))
            m_int = median(p["likes"] + p["replies"] + p["reposts"] for p in own) if own else 0
            er_posts = round(m_int / r["followers"] * 100, 4)
            if not _tol(er_posts, r["engagement_rate"], tol_rate):
                issues.append(Issue(r["handle"], "recompute.engagement_rate", "error", f"stored {r['engagement_rate']} vs raw {er_posts}"))
        else:  # legacy: compare with sum-of-medians definition
            if not _tol(er, r["engagement_rate"], tol_rate):
                issues.append(Issue(r["handle"], "recompute.engagement_rate", "error", f"stored {r['engagement_rate']} vs recomputed {er}"))
        if not _tol(vr, r["views_to_followers"], tol_rate * 10):
            issues.append(Issue(r["handle"], "recompute.views_to_followers", "error", f"stored {r['views_to_followers']} vs recomputed {vr}"))
        if tier_for(r["followers"]) != r["tier"]:
            issues.append(Issue(r["handle"], "recompute.tier", "error", f"stored {r['tier']} vs {tier_for(r['followers'])}"))
    return issues


def check_rules(store: Store, rules: dict | None = None) -> list[Issue]:
    rules = {**DEFAULT_RULES, **(rules or {})}
    issues: list[Issue] = []
    for r in store.latest_measurements():
        h = r["handle"]
        if r["posts_measured"] < rules["min_posts"]:
            issues.append(Issue(h, "rules.min_posts", "error", f"{r['posts_measured']} < {rules['min_posts']}"))
        if r["followers"] < rules["min_followers"]:
            issues.append(Issue(h, "rules.min_followers", "warn", f"{r['followers']} < {rules['min_followers']}"))
        if r["days_since_last_post"] is not None and r["days_since_last_post"] > rules["max_days_since_post"]:
            issues.append(Issue(h, "rules.recency", "warn", f"last post {r['days_since_last_post']}d ago (> {rules['max_days_since_post']})"))
        if rules["require_contact"] and not (r["dm_open"] or (r["bio_url"] or "").strip()):
            issues.append(Issue(h, "rules.contact", "error", "no DM and no bio URL"))
        if not (r["niche"] or "").strip():
            issues.append(Issue(h, "rules.niche", "warn", "niche missing — run `classify` (delivered flagged)"))
    return issues


def check_duplicates(store: Store) -> list[Issue]:
    """Handles are normalized on write, so duplicates can only enter via display/bio_url collisions."""
    rows = store.latest_measurements()
    urls = Counter((r["bio_url"] or "").strip().lower() for r in rows if (r["bio_url"] or "").strip())
    issues = []
    for r in rows:
        u = (r["bio_url"] or "").strip().lower()
        if u and urls[u] > 1:
            issues.append(Issue(r["handle"], "dup.bio_url", "warn", f"bio URL shared by {urls[u]} accounts: {u}"))
    return issues


def check_against_csv(store: Store, csv_path: str | Path, tol_rate: float = 0.006) -> list[Issue]:
    """Cross-check the DB against an externally produced deliverable (e.g. the hand-made
    master CSV): followers, both rates, posts measured. Used to prove the import is faithful."""
    issues: list[Issue] = []
    by_handle = {r["handle"]: r for r in store.latest_measurements()}
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            h = normalize_handle(row["Handle"])
            r = by_handle.get(h)
            if not r:
                issues.append(Issue(h, "csv.missing_in_db", "warn", "row in CSV not in DB"))
                continue
            f = int(row["Followers"].replace(",", ""))
            er = float(next(v for k, v in row.items() if k.startswith("Engagement Rate")).rstrip("%"))
            vr = float(next(v for k, v in row.items() if k.startswith("Views-to-Followers")).rstrip("%"))
            if f != r["followers"]:
                issues.append(Issue(h, "csv.followers", "error", f"csv {f} vs db {r['followers']}"))
            if not _tol(er, r["engagement_rate"], tol_rate):
                issues.append(Issue(h, "csv.engagement_rate", "error", f"csv {er} vs db {r['engagement_rate']}"))
            if not _tol(vr, r["views_to_followers"], tol_rate * 10):
                issues.append(Issue(h, "csv.views_to_followers", "error", f"csv {vr} vs db {r['views_to_followers']}"))
            if int(row["Posts Measured"]) != r["posts_measured"]:
                issues.append(Issue(h, "csv.posts_measured", "error", f"csv {row['Posts Measured']} vs db {r['posts_measured']}"))
    return issues


def run_all(store: Store, rules: dict | None = None, csv_path: str | Path | None = None) -> list[Issue]:
    issues = check_recompute(store) + check_rules(store, rules) + check_duplicates(store)
    if csv_path:
        issues += check_against_csv(store, csv_path)
    return issues


def report(issues: list[Issue], total_rows: int) -> str:
    errors = [i for i in issues if i.severity == "error"]
    warns = [i for i in issues if i.severity == "warn"]
    lines = [f"# Validation report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"rows checked: {total_rows} | errors: {len(errors)} (excluded) | warnings: {len(warns)} (delivered, flagged)", ""]
    by_check = Counter(i.check for i in issues)
    lines += ["| check | count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(by_check.items())]
    if issues:
        lines += ["", "| handle | check | severity | detail |", "|---|---|---|---|"]
        lines += [f"| @{i.handle} | {i.check} | {i.severity} | {i.detail} |" for i in issues]
    return "\n".join(lines) + "\n"
