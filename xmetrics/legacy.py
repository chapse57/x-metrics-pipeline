"""Import the 2026-09 hand-measured deliverable (JSONL of medians) so the pipeline
starts from real data and the validator can prove the numbers match.

Legacy line shape:
{"handle": "@realFatCat1", "f": 3142, "n": 20, "range": "2026-04-21..2026-09-03",
 "mL": 21, "mR": 1, "mRP": 0, "mV": 1238, "dm": true, "url": "...", "bio": "...",
 "niche": "...", "hook_link": "...", "hook_note": "...", "fit": "..."}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .metrics import Summary, rates_from_medians, tier_for
from .store import Store

_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to)\s*(\d{4}-\d{2}-\d{2})")


def parse_range(text: str) -> tuple[str, str]:
    m = _RANGE.search(text or "")
    if not m:
        raise ValueError(f"bad range: {text!r}")
    return m.group(1), m.group(2)


def import_screened_out(store: Store, csv_path: str | Path) -> int:
    """Hand-made exclusion list (Handle, Profile Link, Reason Excluded) -> status='screened_out'."""
    import csv
    n = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if not store.get_account(row["Handle"]):
                store.upsert_account(row["Handle"])
            store.set_status(row["Handle"], "screened_out", row.get("Reason Excluded") or "screened out (legacy)")
            n += 1
    return n


def import_jsonl(store: Store, path: str | Path, *, as_of: datetime | None = None,
                 screened_csv: str | Path | None = None) -> str:
    as_of = as_of or datetime.now(timezone.utc)
    run_id = store.start_run("legacy-import", note=str(path))
    n = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        j = json.loads(line)
        if j.get("EXCLUDED"):  # hand-screened line: {"handle","EXCLUDED":true,"reason"}
            store.upsert_account(j["handle"])
            store.set_status(j["handle"], "screened_out", j.get("reason") or "screened out (legacy)")
            continue
        start, end = parse_range(j["range"])
        er, vr = rates_from_medians(j["f"], j["mL"], j["mR"], j["mRP"], j["mV"])
        last = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        s = Summary(
            followers=int(j["f"]), posts_measured=int(j["n"]), range_start=start, range_end=end,
            median_likes=float(j["mL"]), median_replies=float(j["mR"]), median_reposts=float(j["mRP"]),
            median_views=float(j["mV"]), engagement_rate=er, views_to_followers=vr,
            tier=tier_for(int(j["f"])), days_since_last_post=(as_of - last).days,
        )
        store.upsert_account(
            j["handle"], bio=j.get("bio"), bio_url=j.get("url"), dm_open=1 if j.get("dm") else 0,
            niche=j.get("niche"), niche_source="legacy", fit_note=j.get("fit"),
            hook_link=j.get("hook_link"), hook_note=j.get("hook_note"),
        )
        store.save_measurement(j["handle"], run_id, s)
        n += 1
    if screened_csv:
        import_screened_out(store, screened_csv)
    store.finish_run(run_id)
    return run_id
