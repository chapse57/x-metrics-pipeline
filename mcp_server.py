"""MCP server over the Postgres store, for Claude or any MCP client.

    pip install mcp
    set XMETRICS_MCP_DSN=postgresql://xmetrics_api:...@localhost:5432/xmetrics    (the read-only login)
    set XMETRICS_MCP_KEY=xm_...                     (python -m pg.roles key --name claude-desktop)
    python mcp_server.py                            (stdio transport)

Tools: search_accounts, account, changes, validation_summary, agent_audit. The logic and its tests live in api/mcp_tools.py:
read-only login, every call key-checked and logged (auth.request_log, surface 'mcp'), per-key
limit shared with the HTTP API, at most XMETRICS_MCP_MAX_ROWS rows per answer (default 50,
cut answers say `truncated`).

Until 2026-10 this server read the SQLite file directly, with no key and no cap. The move to
Postgres on 10-02 dropped validation_summary and agent_audit (their data was not in Postgres);
on 10-08 they came back, from raw.agent_audit and mart.v_validation_issues (pg/schema/008).
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from api import mcp_tools as t

mcp = FastMCP("x-metrics")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


@mcp.tool()
def search_accounts(tier: str | None = None, min_engagement: float | None = None,
                    sort: str = "engagement", limit: int = 25) -> str:
    """Latest measurement per account. tier: 'Micro (<25K)' | 'Mid (25-100K)' | 'Macro (100K+)'.
    min_engagement in percent. sort: engagement | followers | views | measured_at | handle (prefix '-' for ascending)."""
    return _j(t.search_accounts(tier, min_engagement, sort, limit))


@mcp.tool()
def account(handle: str) -> str:
    """One account with its measurement history, oldest first."""
    return _j(t.account(handle))


@mcp.tool()
def changes() -> str:
    """What changed in the latest run, flagged rows first."""
    return _j(t.changes())


@mcp.tool()
def validation_summary() -> str:
    """Pre-export checks over the latest measurement of every measured account: rows checked and
    issue counts by check and severity (error = excluded from a deliverable, warn = delivered, flagged)."""
    return _j(t.validation_summary())


@mcp.tool()
def agent_audit() -> str:
    """How the LLM classifier performed behind the guardrails: verdicts and which checks fired."""
    return _j(t.agent_audit())


if __name__ == "__main__":
    mcp.run()
