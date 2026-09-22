-- 001_init.sql — x-metrics on PostgreSQL.
--
-- Three schemas, one rule each:
--   raw   what the collector wrote, column for column, as loaded from the SQLite files.
--         Nothing is corrected here. If a number is wrong in raw, it was wrong at collection.
--   core  the same rows with types fixed and one documented correction (legacy measured_at).
--   mart  what a dashboard or the API reads. Views only — no table here is written by hand,
--         so a dashboard can never show a number that raw cannot reproduce.
--
-- Everything in this file is idempotent: running it twice leaves the database exactly as
-- running it once. tests/test_pg.py asserts that.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

-- ----------------------------------------------------------------------------- raw --
CREATE TABLE IF NOT EXISTS raw.runs (
  run_id       text PRIMARY KEY,
  started_at   timestamptz NOT NULL,
  finished_at  timestamptz,
  source       text NOT NULL,          -- 'playwright' | 'legacy-import'
  note         text,
  source_db    text NOT NULL,          -- which SQLite file this run came from (evidence)
  loaded_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.accounts (
  handle        text PRIMARY KEY,      -- normalized: lowercase, no '@'
  display       text,
  bio           text,
  bio_url       text,
  dm_open       smallint,              -- 1 / 0 / NULL as SQLite stored it
  niche         text,
  niche_source  text,
  fit_note      text,
  hook_link     text,
  hook_note     text,
  status        text NOT NULL,
  screen_reason text,
  created_at    timestamptz NOT NULL,
  updated_at    timestamptz NOT NULL,
  source_db     text NOT NULL,
  loaded_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.measurements (
  handle               text NOT NULL REFERENCES raw.accounts(handle),
  run_id               text NOT NULL REFERENCES raw.runs(run_id),
  measured_at          timestamptz NOT NULL,
  followers            integer NOT NULL,
  posts_measured       integer NOT NULL,
  range_start          date,
  range_end            date,
  median_likes         double precision,
  median_replies       double precision,
  median_reposts       double precision,
  median_views         double precision,
  engagement_rate      double precision NOT NULL,   -- percent, e.g. 0.75 means 0.75 %
  views_to_followers   double precision NOT NULL,   -- percent
  tier                 text NOT NULL,
  days_since_last_post integer,
  loaded_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (handle, run_id)
);

CREATE TABLE IF NOT EXISTS raw.posts (
  handle     text NOT NULL,
  run_id     text NOT NULL REFERENCES raw.runs(run_id),
  status_id  text NOT NULL,
  posted_at  timestamptz NOT NULL,
  replies    integer, reposts integer, likes integer, bookmarks integer, views integer,
  is_repost  smallint, is_pinned smallint, is_reply smallint,
  raw_label  text,
  loaded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (handle, run_id, status_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_measurements_run ON raw.measurements (run_id);
CREATE INDEX IF NOT EXISTS idx_raw_posts_handle_run ON raw.posts (handle, run_id);

-- ---------------------------------------------------------------------------- core --
CREATE OR REPLACE VIEW core.accounts AS
SELECT handle, display, bio, bio_url,
       CASE dm_open WHEN 1 THEN true WHEN 0 THEN false END AS dm_open,
       niche, niche_source, fit_note, hook_link, hook_note, status, screen_reason,
       created_at, updated_at
FROM raw.accounts;

-- The one correction. The 2026-09 legacy import stamped every row with the *import* time
-- (2026-09-07T02:24), not the time the account was measured. The measurement window's end
-- (range_end) is the honest "as of" for those rows. Playwright runs keep their measured_at.
CREATE OR REPLACE VIEW core.measurements AS
SELECT m.handle, m.run_id, r.source, r.started_at AS run_started_at,
       CASE WHEN r.source = 'legacy-import' AND m.range_end IS NOT NULL
            THEN m.range_end::timestamptz
            ELSE m.measured_at END                      AS measured_at,
       m.measured_at                                    AS recorded_at,
       m.followers, m.posts_measured, m.range_start, m.range_end,
       m.median_likes, m.median_replies, m.median_reposts, m.median_views,
       m.engagement_rate, m.views_to_followers, m.tier, m.days_since_last_post
FROM raw.measurements m
JOIN raw.runs r ON r.run_id = m.run_id;

-- ---------------------------------------------------------------------------- mart --

-- Round the way xmetrics.diff._round does: half away from zero on the shortest decimal
-- form of the double. NOTE the ::text hop. A direct float8::numeric cast formats with 15
-- significant digits, so 28.124999999999996 becomes 28.125 and rounds to 28.13, while
-- Python (and this function) say 28.12. float8::text is shortest-round-trip, same as
-- Python's repr(), so both sides round the same digits.
CREATE OR REPLACE FUNCTION mart.round_half_up(x double precision, places int)
RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT round((x::text)::numeric, places)::double precision
$$;

-- Relative change in percent, or NULL when the base is zero — diff._rel.
CREATE OR REPLACE FUNCTION mart.rel_pct(delta double precision, base double precision)
RETURNS double precision
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN base IS NULL OR base = 0 THEN NULL
              ELSE mart.round_half_up(delta / base * 100, 2) END
$$;

-- What changed between two runs. A line-for-line port of xmetrics.diff.compare_rows:
-- same columns, same flag names in the same order, same 'unchanged' rule, same sort.
-- tests/test_pg.py asserts every row equals the Python result for every run pair.
CREATE OR REPLACE FUNCTION mart.changes(
  run_prev            text,
  run_now             text,
  followers_pct       double precision DEFAULT 5.0,
  engagement_pp       double precision DEFAULT 0.2,
  engagement_rel_pct  double precision DEFAULT 25.0,
  views_rel_pct       double precision DEFAULT 25.0,
  silent_days         int              DEFAULT 14
)
RETURNS TABLE (
  handle text, display text, kind text, flags text[],
  followers_prev int, followers_now int, followers_delta int, followers_delta_pct double precision,
  engagement_prev double precision, engagement_now double precision, engagement_delta_pp double precision,
  views_prev double precision, views_now double precision, views_delta_pct double precision,
  tier_prev text, tier_now text,
  days_since_last_post_prev int, days_since_last_post_now int,
  posts_measured_prev int, posts_measured_now int
)
LANGUAGE sql STABLE AS $$
WITH p AS (SELECT * FROM raw.measurements WHERE run_id = run_prev),
     n AS (SELECT * FROM raw.measurements WHERE run_id = run_now),
     j AS (
       SELECT COALESCE(n.handle, p.handle) AS handle,
              COALESCE(a.display, COALESCE(n.handle, p.handle)) AS display,
              p.followers AS followers_prev, n.followers AS followers_now,
              p.engagement_rate AS engagement_prev, n.engagement_rate AS engagement_now,
              p.views_to_followers AS views_prev, n.views_to_followers AS views_now,
              p.tier AS tier_prev, n.tier AS tier_now,
              p.days_since_last_post AS dslp_prev, n.days_since_last_post AS dslp_now,
              p.posts_measured AS pm_prev, n.posts_measured AS pm_now,
              (p.handle IS NULL) AS is_new, (n.handle IS NULL) AS is_dropped
       FROM p FULL OUTER JOIN n ON n.handle = p.handle
       LEFT JOIN raw.accounts a ON a.handle = COALESCE(n.handle, p.handle)
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
     ),
     k AS (
       SELECT f.*,
              CASE WHEN is_new THEN 'new'
                   WHEN is_dropped THEN 'dropped'
                   WHEN cardinality(flags) = 0 AND followers_delta = 0 AND engagement_delta_pp = 0
                        AND (views_delta_pct IS NULL OR views_delta_pct = 0) THEN 'unchanged'
                   ELSE 'changed' END AS kind
       FROM f
     )
SELECT handle, display, kind, flags,
       followers_prev, followers_now, followers_delta, followers_delta_pct,
       engagement_prev, engagement_now, engagement_delta_pp,
       views_prev, views_now, views_delta_pct,
       tier_prev, tier_now, dslp_prev, dslp_now, pm_prev, pm_now
FROM k
ORDER BY CASE kind WHEN 'dropped' THEN 0 WHEN 'new' THEN 1 WHEN 'changed' THEN 2 ELSE 3 END,
         CASE WHEN cardinality(flags) > 0 THEN 0 ELSE 1 END,
         -abs(COALESCE(followers_delta_pct, 0)),
         handle
$$;

-- Runs that actually produced measurements, ordered like diff.runs_with_measurements:
-- by the runs table's started_at, not by run_id string.
CREATE OR REPLACE VIEW mart.v_runs AS
SELECT r.run_id, r.started_at, r.finished_at, r.source, r.note,
       count(m.handle)::int AS accounts_measured,
       row_number() OVER (ORDER BY r.started_at DESC, r.run_id DESC)::int AS recency
FROM raw.runs r
JOIN raw.measurements m ON m.run_id = r.run_id
GROUP BY r.run_id;

-- The latest two runs compared — what diff.compare(store) returns with no arguments.
CREATE OR REPLACE VIEW mart.v_changes AS
SELECT c.*
FROM (SELECT run_id FROM mart.v_runs WHERE recency = 1) now_r,
     (SELECT run_id FROM mart.v_runs WHERE recency = 2) prev_r,
     LATERAL mart.changes(prev_r.run_id, now_r.run_id) c;

-- Each account's most recent measurement (by run start, like the diff), with the account's
-- current label and status alongside. This is the table view on the dashboard.
CREATE OR REPLACE VIEW mart.v_latest AS
SELECT DISTINCT ON (m.handle)
       m.handle, a.display, a.niche, a.status, a.dm_open,
       m.run_id, m.source, m.measured_at,
       m.followers, m.tier, m.engagement_rate, m.views_to_followers,
       m.posts_measured, m.days_since_last_post,
       m.median_likes, m.median_replies, m.median_reposts, m.median_views
FROM core.measurements m
JOIN core.accounts a ON a.handle = m.handle
ORDER BY m.handle, m.run_started_at DESC, m.run_id DESC;

-- When is this data from? One row per account plus the age in days, so a dashboard card
-- can show "as of" and turn red. Legacy rows report their measurement window's end, not the
-- day they were imported (see core.measurements).
CREATE OR REPLACE VIEW mart.v_freshness AS
SELECT handle, display, source, run_id,
       measured_at                                                   AS last_measured_at,
       (now() - measured_at)                                         AS age,
       floor(EXTRACT(EPOCH FROM now() - measured_at) / 86400)::int   AS age_days
FROM mart.v_latest;
