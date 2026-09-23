"""Every SQL statement the API runs, in one place, complete. Nothing here is assembled from
request input: filters are bind parameters, and the only thing a client can choose about
the shape of a query — the sort — is a key into SORTS, whose values are fixed fragments.
The API reads views and mart functions only (mart.v_*, core.measurements); it never names a raw table.
"""
from __future__ import annotations

# ---- /accounts ----------------------------------------------------------------------
# key the client sends -> ORDER BY fragment. Anything else is a 422 before SQL is touched.
SORTS: dict[str, str] = {
    "followers":        "followers DESC NULLS LAST, handle COLLATE \"C\"",
    "-followers":       "followers ASC NULLS LAST, handle COLLATE \"C\"",
    "engagement":       "engagement_rate DESC NULLS LAST, handle COLLATE \"C\"",
    "-engagement":      "engagement_rate ASC NULLS LAST, handle COLLATE \"C\"",
    "views":            "views_to_followers DESC NULLS LAST, handle COLLATE \"C\"",
    "-views":           "views_to_followers ASC NULLS LAST, handle COLLATE \"C\"",
    "measured_at":      "measured_at DESC, handle COLLATE \"C\"",
    "-measured_at":     "measured_at ASC, handle COLLATE \"C\"",
    "handle":           "handle COLLATE \"C\"",
}
DEFAULT_SORT = "engagement"

TIERS = ("Micro (<25K)", "Mid (25-100K)", "Macro (100K+)")

ACCOUNTS = """
SELECT handle, display, niche, status, dm_open, run_id, source, measured_at,
       followers, tier, engagement_rate, views_to_followers, posts_measured, days_since_last_post,
       count(*) OVER () AS total
FROM mart.v_latest
WHERE (%(tier)s::text IS NULL OR tier = %(tier)s)
  AND (%(min_engagement)s::float8 IS NULL OR engagement_rate >= %(min_engagement)s)
  AND (%(status)s::text IS NULL OR status = %(status)s)
ORDER BY {order_by}
LIMIT %(limit)s OFFSET %(offset)s
"""

ACCOUNT = """
SELECT handle, display, niche, status, dm_open, run_id, source, measured_at,
       followers, tier, engagement_rate, views_to_followers, posts_measured, days_since_last_post,
       median_likes, median_replies, median_reposts, median_views
FROM mart.v_latest WHERE handle = %(handle)s
"""

# an account's measurements over time, oldest first, on the corrected clock (core re-dates legacy rows)
HISTORY = """
SELECT run_id, source, measured_at, followers, tier, engagement_rate, views_to_followers,
       posts_measured, days_since_last_post
FROM core.measurements WHERE handle = %(handle)s
ORDER BY measured_at, run_id
"""

# ---- /changes -----------------------------------------------------------------------
CHANGES_LATEST = "SELECT * FROM mart.v_changes"
CHANGES_FOR_RUN = "SELECT * FROM mart.changes_since(%(run)s)"
RUN_EXISTS = "SELECT 1 FROM mart.v_runs WHERE run_id = %(run)s"

# ---- /runs, /health -----------------------------------------------------------------
RUNS = """
SELECT run_id, source, note, started_at, finished_at, taken_at,
       targets, measured, missing, failed, not_reached, complete
FROM mart.v_run_status ORDER BY taken_at DESC, run_id DESC LIMIT %(limit)s
"""

LATEST_RUN = """
SELECT run_id, source, taken_at, targets, accounts_measured AS measured, missing, failed, not_reached, complete,
       EXTRACT(EPOCH FROM now() - taken_at)::bigint AS age_seconds
FROM mart.v_runs WHERE recency = 1
"""
