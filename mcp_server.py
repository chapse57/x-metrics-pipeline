"""Minimal MCP server exposing the measured dataset to Claude (or any MCP client).

    pip install mcp
    python mcp_server.py --db out/xmetrics.db          # stdio transport

Tools:
  search_accounts(niche?, tier?, min_engagement?, limit)  -> rows from the latest measurements
  account(handle)                                          -> one account with its raw posts
  validation_summary()                                     -> issue counts by check
  agent_audit()                                            -> guardrail statistics

Read-only by design: the server never writes to the store.
"""
from __future__ import annotations

import argparse
import json

from mcp.server.fastmcp import FastMCP

from xmetrics.store import Store
from xmetrics.validate import run_all

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="out/xmetrics.db")
ARGS, _ = ap.parse_known_args()
mcp = FastMCP("x-metrics")


def _rows(rows):
    return [dict(r) for r in rows]


@mcp.tool()
def search_accounts(niche: str | None = None, tier: str | None = None, min_engagement: float = 0.0, limit: int = 25) -> str:
    """Search measured X accounts. tier: 'Micro (<25K)' | 'Mid (25-100K)' | 'Macro (100K+)'. min_engagement in percent."""
    st = Store(ARGS.db)
    out = []
    for r in st.latest_measurements():
        if niche and niche.lower() not in (r["niche"] or "").lower():
            continue
        if tier and r["tier"] != tier:
            continue
        if r["engagement_rate"] < min_engagement:
            continue
        out.append({k: r[k] for k in ("display", "followers", "engagement_rate", "views_to_followers", "tier", "niche", "bio_url", "dm_open", "range_end")})
        if len(out) >= limit:
            break
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def account(handle: str) -> str:
    """One account with its latest measurement and raw per-post counts (if collected by the pipeline)."""
    st = Store(ARGS.db)
    a = st.get_account(handle)
    if not a:
        return json.dumps({"error": "unknown handle"})
    m = st.conn.execute("SELECT * FROM measurements WHERE handle=? ORDER BY measured_at DESC LIMIT 1", (a["handle"],)).fetchone()
    posts = st.posts_for(a["handle"], m["run_id"]) if m else []
    return json.dumps({"account": dict(a), "measurement": dict(m) if m else None, "posts": _rows(posts)}, ensure_ascii=False)


@mcp.tool()
def validation_summary() -> str:
    """Counts of validation issues by check and severity for the current dataset."""
    st = Store(ARGS.db)
    issues = run_all(st)
    agg: dict[str, int] = {}
    for i in issues:
        agg[f"{i.check} ({i.severity})"] = agg.get(f"{i.check} ({i.severity})", 0) + 1
    return json.dumps({"rows": len(st.latest_measurements()), "issues": agg})


@mcp.tool()
def agent_audit() -> str:
    """How the LLM classifier performed behind the guardrails: verdicts and which checks fired."""
    return json.dumps(Store(ARGS.db).agent_audit_summary())


if __name__ == "__main__":
    mcp.run()
