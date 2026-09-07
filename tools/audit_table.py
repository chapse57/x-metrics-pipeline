"""Turn agent_audit rows into the README's "what the model got wrong" table.

The point of this script: for every answer the guardrail refused, show *what the
model actually said* and *which deterministic check caught it* — reconstructed
from the same corpus the model was shown (bio + stored post texts), so an
`evidence` failure can name the exact quote that was not in the source.

    python tools/audit_table.py --db out/x_claude.db
    python tools/audit_table.py --db out/x_claude.db --classifier rule   # compare
    python tools/audit_table.py --db out/x_claude.db --raw 3            # dump 3 full raw outputs
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

CHECK_MEANING = {
    "schema": "not valid JSON / missing keys / wrong types",
    "label_set": "niche outside the closed set (invented category)",
    "evidence": "evidence quote is not a verbatim substring of the input",
    "confidence": "confidence below threshold",
    "rule_conflict": "disagrees with the rule-based spam screen",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def corpus_for(conn: sqlite3.Connection, handle: str) -> str:
    bio = conn.execute("SELECT bio FROM accounts WHERE handle=?", (handle,)).fetchone()
    bio = (bio[0] if bio else "") or ""
    has_texts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='post_texts'").fetchone()
    run = conn.execute("SELECT MAX(run_id) FROM post_texts WHERE handle=?", (handle,)).fetchone() if has_texts else None
    posts: list[str] = []
    if run and run[0]:
        posts = [r[0] for r in conn.execute(
            "SELECT text FROM post_texts WHERE handle=? AND run_id=? ORDER BY idx", (handle, run[0])
        )]
    return "\n".join([bio, *posts])


def offending(parsed: dict | None, failed: list[str], corpus: str) -> str:
    """One sentence naming the concrete thing that failed — not just the check name."""
    if parsed is None:
        return "output did not contain a JSON object"
    bits: list[str] = []
    c = norm(corpus)
    if "label_set" in failed:
        bits.append(f'niche = "{parsed.get("niche")}" (not in the closed set)')
    if "evidence" in failed:
        quotes = [q for q in parsed.get("evidence", []) if isinstance(q, str) and q.strip()]
        if not quotes:
            bits.append("no evidence quote returned")
        else:
            bad = [q for q in quotes if norm(q) not in c]
            for q in bad[:2]:
                bits.append(f'quote not in source: "{q[:120]}"')
    if "confidence" in failed:
        bits.append(f'confidence = {parsed.get("confidence")}')
    if "rule_conflict" in failed:
        bits.append(f'model is_spam = {parsed.get("is_spam")}, rule screen disagrees')
    if "schema" in failed and not bits:
        bits.append("keys/types did not match the required schema")
    return "; ".join(bits) or "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="out/x_claude.db")
    ap.add_argument("--classifier", default="claude", help="'claude' (prefix match), 'rule', or 'all'")
    ap.add_argument("--raw", type=int, default=0, help="also dump N full raw model outputs that were refused")
    ap.add_argument("--limit", type=int, default=25, help="max rows in the table")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    if a.classifier == "all":
        where, args = "1=1", ()
    elif a.classifier == "claude":
        where, args = "classifier LIKE 'claude:%'", ()
    else:
        where, args = "classifier = ?", (a.classifier,)

    rows = conn.execute(
        f"SELECT * FROM agent_audit WHERE {where} ORDER BY id", args
    ).fetchall()
    if not rows:
        print(f"no agent_audit rows for classifier={a.classifier} in {a.db}", file=sys.stderr)
        return 1

    by_verdict: dict[str, int] = {}
    fired: dict[str, int] = {}
    models: dict[str, int] = {}
    for r in rows:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        models[r["classifier"]] = models.get(r["classifier"], 0) + 1
        for c in json.loads(r["failed_checks"] or "[]"):
            fired[c] = fired.get(c, 0) + 1

    print("```json")
    print(json.dumps({"attempts": len(rows), "by_verdict": by_verdict,
                      "guardrail_fired": fired, "classifiers": models}, indent=1))
    print("```\n")

    refused = [r for r in rows if r["verdict"] != "accepted"]
    print(f"## What the model got wrong ({len(refused)} of {len(rows)} answers refused)\n")
    print("| account | verdict | check | what the model said |")
    print("|---|---|---|---|")
    for r in refused[: a.limit]:
        failed = json.loads(r["failed_checks"] or "[]")
        parsed = json.loads(r["parsed"]) if r["parsed"] else None
        detail = offending(parsed, failed, corpus_for(conn, r["handle"]))
        detail = detail.replace("|", "\\|").replace("\n", " ")
        print(f'| @{r["handle"]} | {r["verdict"]} | `{"`, `".join(failed)}` | {detail} |')

    print("\n| check | catches |")
    print("|---|---|")
    for c, n in sorted(fired.items(), key=lambda kv: -kv[1]):
        print(f"| `{c}` ({n}) | {CHECK_MEANING.get(c, '')} |")

    for r in refused[: a.raw]:
        print(f'\n### raw output — @{r["handle"]} ({r["verdict"]}: {", ".join(json.loads(r["failed_checks"] or "[]"))})\n')
        print("```")
        print((r["raw_output"] or "").strip()[:1200])
        print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
