-- 002_run_scope.sql — what changed, per account, with run scope.
--
-- 001 compared the two latest runs as whole sets. That is wrong as soon as runs cover
-- different accounts: after the 95-account legacy import, a 3-account live run showed 92
-- "dropped" accounts that nobody had dropped — they were not tried. The rule now, the same
-- rule xmetrics.diff.compare implements in Python (tests/test_pg.py asserts row equality):
--
--   Each account is compared with its own previous measurement, and an account counts as
--   "dropped" only when a run that was meant to cover it came back without it.
--
-- "Meant to cover it" is raw.runs.scope: 'full' = the run tried every tracked account
-- (a first collection, `xmetrics collect --all`, the legacy import); 'partial' = a named
-- subset. The collector decides it from what it is about to measure, not from a flag.
--
-- Objects: raw.runs.scope · mart.change (row type) · mart.diff_row (one account, the whole
-- rule) · mart.changes (two named runs, whole sets — kept for "exactly these two") ·
-- mart.changes_since (one run vs each account's baseline) · mart.v_changes (the latest run).
-- Applied once by pg/migrate.py; written to be re-runnable by hand all the same.

ALTER TABLE raw.runs ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'full';
ALTER TABLE raw.runs DROP CONSTRAINT IF EXISTS runs_scope_check;
ALTER TABLE raw.runs ADD CONSTRAINT runs_scope_check CHECK (scope IN ('full', 'partial'));

-- core.measurements' legacy re-dating, now at an explicit UTC midnight rather than the session
-- time zone's, because the baseline order below compares it with Python's string
-- (range_end || 'T00:00:00+00:00') and the two must sort the same.
CREATE OR REPLACE VIEW core.measurements AS
SELECT m.handle, m.run_id, r.source, r.started_at AS run_started_at,
       CASE WHEN r.source = 'legacy-import' AND m.range_end IS NOT NULL
            THEN (m.range_end::text || 'T00:00:00+00:00')::timestamptz
            ELSE m.measured_at END                      AS measured_at,
       m.measured_at                                    AS recorded_at,
       m.followers, m.posts_measured, m.range_start, m.range_end,
       m.median_likes, m.median_replies, m.median_reposts, m.median_views,
       m.engagement_rate, m.views_to_followers, m.tier, m.days_since_last_post
FROM raw.measurements m
JOIN raw.runs r ON r.run_id = m.run_id;

DROP VIEW IF EXISTS mart.v_changes;
DROP VIEW IF EXISTS mart.v_runs;
DROP FUNCTION IF EXISTS mart.changes(text, text, double precision, double precision, double precision, double precision, int);
DROP FUNCTION IF EXISTS mart.changes_since(text, double precision, double precision, double precision, double precision, int);
DROP FUNCTION IF EXISTS mart.diff_row(raw.measurements, raw.measurements, text, double precision, double precision, double precision, double precision, int);
DROP TYPE IF EXISTS mart.change;

-- One row of the change report — xmetrics.diff.Change, field for field, same order.
CREATE TYPE mart.change AS (
  handle text, display text, kind text, flags text[],
  followers_prev int, followers_now int, followers_delta int, followers_delta_pct double precision,
  engagement_prev double precision, engagement_now double precision, engagement_delta_pp double precision,
  views_prev double precision, views_now double precision, views_delta_pct double precision,
  tier_prev text, tier_now text,
  days_since_last_post_prev int, days_since_last_post_now int,
  posts_measured_prev int, posts_measured_now int,
  run_prev text, run_now text
);

-- The whole rule for one account: xmetrics.diff.compare_rows, line for line. p or n may be
-- NULL (a row of NULLs from an outer join counts as NULL here): no p = new, no n = dropped.
CREATE FUNCTION mart.diff_row(
  p raw.measurements, n raw.measurements, display text,
  followers_pct double precision, engagement_pp double precision, engagement_rel_pct double precision,
  views_rel_pct double precision, silent_days int
) RETURNS mart.change
LANGUAGE sql IMMUTABLE AS $$
WITH j AS (
  SELECT COALESCE((n).handle, (p).handle) AS handle,
         COALESCE(display, (n).handle, (p).handle) AS display,
         (p).handle IS NULL AS is_new, (n).handle IS NULL AS is_dropped,
         (p).followers AS followers_prev, (n).followers AS followers_now,
         (p).engagement_rate AS engagement_prev, (n).engagement_rate AS engagement_now,
         (p).views_to_followers AS views_prev, (n).views_to_followers AS views_now,
         (p).tier AS tier_prev, (n).tier AS tier_now,
         (p).days_since_last_post AS dslp_prev, (n).days_since_last_post AS dslp_now,
         (p).posts_measured AS pm_prev, (n).posts_measured AS pm_now,
         (p).run_id AS run_prev, (n).run_id AS run_now
),
d AS (
  SELECT j.*,
         CASE WHEN NOT is_new AND NOT is_dropped THEN followers_now - followers_prev END AS followers_delta,
         CASE WHEN NOT is_new AND NOT is_dropped
              THEN mart.rel_pct((followers_now - followers_prev)::double precision, followers_prev::double precision) END AS followers_delta_pct,
         CASE WHEN NOT is_new AND NOT is_dropped
              THEN mart.round_half_up(engagement_now - engagement_prev, 4) END AS engagement_delta_pp,
         CASE WHEN NOT is_new AND NOT is_dropped
              THEN mart.rel_pct(views_now - views_prev, views_prev) END AS views_delta_pct
  FROM j
),
f AS (
  SELECT d.*,
         CASE WHEN is_new OR is_dropped THEN ARRAY[]::text[] ELSE
           ARRAY_REMOVE(ARRAY[
             CASE WHEN followers_delta_pct IS NOT NULL AND abs(followers_delta_pct) >= followers_pct
                  THEN CASE WHEN followers_delta > 0 THEN 'followers_up' ELSE 'followers_down' END END,
             CASE WHEN abs(engagement_delta_pp) >= engagement_pp
                   AND (mart.rel_pct(engagement_delta_pp, engagement_prev) IS NULL
                        OR abs(mart.rel_pct(engagement_delta_pp, engagement_prev)) >= engagement_rel_pct)
                  THEN CASE WHEN engagement_delta_pp > 0 THEN 'engagement_up' ELSE 'engagement_down' END END,
             CASE WHEN views_delta_pct IS NOT NULL AND abs(views_delta_pct) >= views_rel_pct
                  THEN CASE WHEN views_delta_pct > 0 THEN 'views_up' ELSE 'views_down' END END,
             CASE WHEN tier_prev IS DISTINCT FROM tier_now THEN 'tier_change' END,
             CASE WHEN dslp_prev IS NOT NULL AND dslp_now IS NOT NULL THEN
                    CASE WHEN dslp_prev < silent_days AND silent_days <= dslp_now THEN 'went_silent'
                         WHEN dslp_now < silent_days AND silent_days <= dslp_prev THEN 'active_again' END END
           ], NULL) END AS flags
  FROM d
)
SELECT ROW(handle, display,
           CASE WHEN is_new THEN 'new'
                WHEN is_dropped THEN 'dropped'
                WHEN cardinality(flags) = 0 AND followers_delta = 0 AND engagement_delta_pp = 0
                     AND (views_delta_pct IS NULL OR views_delta_pct = 0) THEN 'unchanged'
                ELSE 'changed' END,
           flags,
           followers_prev, followers_now, followers_delta, followers_delta_pct,
           engagement_prev, engagement_now, engagement_delta_pp,
           views_prev, views_now, views_delta_pct,
           tier_prev, tier_now, dslp_prev, dslp_now, pm_prev, pm_now,
           run_prev, run_now)::mart.change
FROM f
$$;

-- Two named runs as whole sets: diff.compare(store, run_prev, run_now). Absence on either
-- side is reported (new / dropped) regardless of scope — the caller asked for these two.
CREATE FUNCTION mart.changes(
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
         handle
$$;

-- One run against each account's own previous measurement: diff.compare(store, run_now=...).
-- Baseline = the account's latest measurement taken before this run's measurements — by
-- measurement time (core.measurements.measured_at, which re-dates legacy rows), not by run
-- start, because an import runs after the numbers it carries were taken. Ties by run_id.
-- Screened-out accounts are never a baseline. 'dropped' rows appear only when scope is 'full'.
CREATE FUNCTION mart.changes_since(
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
         handle
$$;

-- Runs that produced measurements, newest first, now with their scope (dashboard run picker).
CREATE VIEW mart.v_runs AS
SELECT r.run_id, r.started_at, r.finished_at, r.source, r.note,
       count(m.handle)::int AS accounts_measured,
       row_number() OVER (ORDER BY r.started_at DESC, r.run_id DESC)::int AS recency,
       r.scope
FROM raw.runs r
JOIN raw.measurements m ON m.run_id = r.run_id
GROUP BY r.run_id;

-- What changed in the latest run — the dashboard's first screen. Empty until there are two
-- runs with measurements, like diff.compare(store).
CREATE VIEW mart.v_changes AS
SELECT c.*
FROM (SELECT run_id FROM mart.v_runs WHERE recency = 1) now_r
CROSS JOIN LATERAL mart.changes_since(now_r.run_id) c
WHERE (SELECT count(*) FROM mart.v_runs) >= 2;
