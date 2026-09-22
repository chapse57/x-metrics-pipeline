-- 003_handle_order.sql — break ties by handle in byte order, like Python does.
--
-- mart.changes / mart.changes_since end their ORDER BY with `handle`. That uses the
-- database's collation, and the official postgres image initialises with en_US.utf8,
-- which ignores punctuation at first pass: "aaronrentfrew" sorts before "_amtrades".
-- Python's sorted() compares code points, so "_" (0x5F) comes before "a" (0x61).
-- Same rows, different order — and tests/test_pg.py compares row by row, so it failed
-- on a Docker database while passing on a C-locale one.
--
-- COLLATE "C" is code-point order for these ASCII handles, on any server locale.
-- Only the two function bodies change; signatures and return type stay, so the views
-- that read them are left alone.

CREATE OR REPLACE FUNCTION mart.changes(
  run_prev text, run_now text,
  followers_pct double precision DEFAULT 5.0, engagement_pp double precision DEFAULT 0.2,
  engagement_rel_pct double precision DEFAULT 25.0, views_rel_pct double precision DEFAULT 25.0,
  silent_days int DEFAULT 14
) RETURNS SETOF mart.change
LANGUAGE sql STABLE AS $$
WITH p AS (SELECT m AS row, m.handle FROM raw.measurements m WHERE m.run_id = run_prev),
     n AS (SELECT m AS row, m.handle FROM raw.measurements m WHERE m.run_id = run_now),
     report AS (
       SELECT c.*
       FROM p FULL OUTER JOIN n ON n.handle = p.handle
       LEFT JOIN raw.accounts a ON a.handle = COALESCE(n.handle, p.handle)
       CROSS JOIN LATERAL mart.diff_row(p.row, n.row, a.display,
                                        followers_pct, engagement_pp, engagement_rel_pct, views_rel_pct, silent_days) c
     )
SELECT * FROM report
ORDER BY CASE kind WHEN 'dropped' THEN 0 WHEN 'new' THEN 1 WHEN 'changed' THEN 2 ELSE 3 END,
         CASE WHEN cardinality(flags) > 0 THEN 0 ELSE 1 END,
         -abs(COALESCE(followers_delta_pct, 0)),
         handle COLLATE "C"
$$;

-- One run against each account's own previous measurement: diff.compare(store, run_now=...).
-- Baseline = the account's latest measurement taken before this run's measurements — by
-- measurement time (core.measurements.measured_at, which re-dates legacy rows), not by run
-- start, because an import runs after the numbers it carries were taken. Ties by run_id.
-- Screened-out accounts are never a baseline. 'dropped' rows appear only when scope is 'full'.
CREATE OR REPLACE FUNCTION mart.changes_since(
  run_now text,
  followers_pct double precision DEFAULT 5.0, engagement_pp double precision DEFAULT 0.2,
  engagement_rel_pct double precision DEFAULT 25.0, views_rel_pct double precision DEFAULT 25.0,
  silent_days int DEFAULT 14
) RETURNS SETOF mart.change
LANGUAGE sql STABLE AS $$
WITH nowr AS (SELECT run_id, scope FROM raw.runs WHERE run_id = run_now),
     cut AS (SELECT min(measured_at) AS cutoff FROM core.measurements WHERE run_id = run_now),
     n AS (SELECT m AS row, m.handle FROM raw.measurements m WHERE m.run_id = run_now),
     earlier AS (
       SELECT m AS row, m.handle,
              row_number() OVER (PARTITION BY m.handle ORDER BY c.measured_at DESC, m.run_id DESC) AS rn
       FROM raw.measurements m
       JOIN core.measurements c ON c.handle = m.handle AND c.run_id = m.run_id
       CROSS JOIN cut
       WHERE m.run_id <> run_now AND c.measured_at < cut.cutoff
     ),
     base AS (
       SELECT e.row, e.handle FROM earlier e
       JOIN raw.accounts a ON a.handle = e.handle
       WHERE e.rn = 1 AND a.status <> 'screened_out'
     ),
     report AS (
       -- every account measured now, against its baseline (no baseline = new)
       SELECT c.*
       FROM n LEFT JOIN base b ON b.handle = n.handle
       LEFT JOIN raw.accounts a ON a.handle = n.handle
       CROSS JOIN LATERAL mart.diff_row(b.row, n.row, a.display,
                                        followers_pct, engagement_pp, engagement_rel_pct, views_rel_pct, silent_days) c
       UNION ALL
       -- tried, came back empty — only meaningful when everything was tried
       SELECT c.*
       FROM base b
       LEFT JOIN raw.accounts a ON a.handle = b.handle
       CROSS JOIN nowr
       CROSS JOIN LATERAL mart.diff_row(b.row, NULL::raw.measurements, a.display,
                                        followers_pct, engagement_pp, engagement_rel_pct, views_rel_pct, silent_days) c
       WHERE nowr.scope = 'full' AND NOT EXISTS (SELECT 1 FROM n WHERE n.handle = b.handle)
     )
SELECT * FROM report
ORDER BY CASE kind WHEN 'dropped' THEN 0 WHEN 'new' THEN 1 WHEN 'changed' THEN 2 ELSE 3 END,
         CASE WHEN cardinality(flags) > 0 THEN 0 ELSE 1 END,
         -abs(COALESCE(followers_delta_pct, 0)),
         handle COLLATE "C"
$$;
