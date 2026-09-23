-- 004_run_targets.sql — "dropped" means we went looking and it was not there.
--
-- 002 introduced runs.scope, a one-word summary (full / partial) of what a run tried. It was
-- decided from one SQLite file's account list, so the same 3-account run was "full" in its own
-- file and wrong once merged with the 148-account import: the latest run showed 92 accounts
-- "dropped" that nobody had tried. The value got hand-patched to make the view look right,
-- which is exactly what a summary column invites.
--
-- The summary is gone. In its place, one row per (run, account) the run set out to measure,
-- with what became of it — the same table xmetrics.store keeps:
--
--   pending   the run never reached it (it died first)        -> the run is incomplete; /health says so
--   measured  a number came back                               -> compared with the account's previous one
--   missing   we got there; the account is gone or suspended  -> "dropped", if it had a previous measurement
--   error     we got there; could not read it                  -> named in the report, never a change
--
-- Two of those are facts about the account; two are facts about the collector. The old design
-- folded all four into "absent", and a crashed run would have reported every unreached account
-- as dropped. That is the worst kind of wrong: the number is not merely off, the reason it is
-- off is hidden.
--
-- As it is said to a client: "This dashboard never reports an account as gone unless we
-- actually went looking for it and it wasn't there. If we didn't look, it says so."
--
-- Baselines are now per account, not per run: each account's previous value is its latest
-- measurement taken before *its own* measurement in this run. A run's rows can span days (an
-- import carries each account's own window), so one run-level cutoff picked the wrong baseline
-- for some accounts. Run order (mart.v_runs) follows the same clock — when the numbers were
-- taken, not when the run started; a run with no measurements falls back to its start.
--
-- Runs loaded from files that predate run_targets get targets = measured, all 'measured'
-- (pg/load.py reconstructs them the same way xmetrics.store does on open): nothing is called
-- missing or unreached for a run we cannot know that about.

CREATE TABLE IF NOT EXISTS raw.run_targets (
  run_id     text NOT NULL REFERENCES raw.runs(run_id),
  handle     text NOT NULL,
  outcome    text NOT NULL,
  detail     text,
  updated_at timestamptz NOT NULL,
  loaded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, handle),
  CONSTRAINT run_targets_outcome_check CHECK (outcome IN ('pending', 'measured', 'missing', 'error'))
);
CREATE INDEX IF NOT EXISTS idx_raw_run_targets_handle ON raw.run_targets (handle);

-- The views read runs.scope; drop them before the column, recreate them after.
DROP VIEW IF EXISTS mart.v_changes;
DROP VIEW IF EXISTS mart.v_run_status;
DROP VIEW IF EXISTS mart.v_runs;
DROP FUNCTION IF EXISTS mart.changes_since(text, double precision, double precision, double precision, double precision, int);
ALTER TABLE raw.runs DROP CONSTRAINT IF EXISTS runs_scope_check;
ALTER TABLE raw.runs DROP COLUMN IF EXISTS scope;

-- One run against each account's own previous measurement: diff.compare(store, run_now=...).
CREATE FUNCTION mart.changes_since(
  run_now text,
  followers_pct double precision DEFAULT 5.0, engagement_pp double precision DEFAULT 0.2,
  engagement_rel_pct double precision DEFAULT 25.0, views_rel_pct double precision DEFAULT 25.0,
  silent_days int DEFAULT 14
) RETURNS SETOF mart.change
LANGUAGE sql STABLE AS $$
WITH r AS (SELECT run_id, started_at FROM raw.runs WHERE run_id = run_now),
     n AS (   -- measured in this run, each with its own "as of"
       SELECT m AS row, m.handle, c.measured_at AS as_of
       FROM raw.measurements m
       JOIN core.measurements c ON c.handle = m.handle AND c.run_id = m.run_id
       WHERE m.run_id = run_now),
     gone AS ( -- went looking, not there, and no number came back
       SELECT t.handle, r.started_at AS as_of
       FROM raw.run_targets t CROSS JOIN r
       WHERE t.run_id = run_now AND t.outcome = 'missing'
         AND NOT EXISTS (SELECT 1 FROM n WHERE n.handle = t.handle)),
     subjects AS (SELECT handle, as_of FROM n UNION ALL SELECT handle, as_of FROM gone),
     earlier AS (
       SELECT s.handle, m AS row,
              row_number() OVER (PARTITION BY s.handle ORDER BY c.measured_at DESC, m.run_id DESC) AS rn
       FROM subjects s
       JOIN raw.measurements m ON m.handle = s.handle AND m.run_id <> run_now
       JOIN core.measurements c ON c.handle = m.handle AND c.run_id = m.run_id
       WHERE c.measured_at < s.as_of),
     base AS (SELECT handle, row FROM earlier WHERE rn = 1),
     report AS (
       SELECT c.*
       FROM n LEFT JOIN base b ON b.handle = n.handle
       LEFT JOIN raw.accounts a ON a.handle = n.handle
       CROSS JOIN LATERAL mart.diff_row(b.row, n.row, a.display,
                                        followers_pct, engagement_pp, engagement_rel_pct, views_rel_pct, silent_days) c
       UNION ALL
       SELECT c.*
       FROM gone g JOIN base b ON b.handle = g.handle      -- nothing to lose = nothing to report
       LEFT JOIN raw.accounts a ON a.handle = g.handle
       CROSS JOIN LATERAL mart.diff_row(b.row, NULL::raw.measurements, a.display,
                                        followers_pct, engagement_pp, engagement_rel_pct, views_rel_pct, silent_days) c
     )
SELECT * FROM report
ORDER BY CASE kind WHEN 'dropped' THEN 0 WHEN 'new' THEN 1 WHEN 'changed' THEN 2 ELSE 3 END,
         CASE WHEN cardinality(flags) > 0 THEN 0 ELSE 1 END,
         -abs(COALESCE(followers_delta_pct, 0)),
         handle COLLATE "C"
$$;

-- Every run, with what it set out to do and how far it got. This is the /health and ops view:
-- a run with pending targets is incomplete, whatever its finished_at says.
CREATE VIEW mart.v_run_status AS
SELECT r.run_id, r.started_at, r.finished_at, r.source, r.note,
       COALESCE(c.taken_at, r.started_at) AS taken_at,
       t.targets, t.measured, t.missing, t.failed, t.not_reached,
       (t.not_reached = 0) AS complete
FROM raw.runs r
CROSS JOIN LATERAL (
  SELECT count(*)::int                                     AS targets,
         count(*) FILTER (WHERE outcome = 'measured')::int AS measured,
         count(*) FILTER (WHERE outcome = 'missing')::int  AS missing,
         count(*) FILTER (WHERE outcome = 'error')::int    AS failed,
         count(*) FILTER (WHERE outcome = 'pending')::int  AS not_reached
  FROM raw.run_targets WHERE run_id = r.run_id) t
CROSS JOIN LATERAL (
  SELECT max(measured_at) AS taken_at FROM core.measurements WHERE run_id = r.run_id) c;

-- Runs that produced measurements, newest first by when their numbers were taken — the same
-- order diff.runs_with_measurements uses. The dashboard's run picker.
CREATE VIEW mart.v_runs AS
SELECT s.run_id, s.started_at, s.finished_at, s.source, s.note, s.taken_at,
       s.measured AS accounts_measured, s.targets, s.missing, s.failed, s.not_reached, s.complete,
       row_number() OVER (ORDER BY s.taken_at DESC, s.run_id DESC)::int AS recency
FROM mart.v_run_status s
WHERE EXISTS (SELECT 1 FROM raw.measurements m WHERE m.run_id = s.run_id);

-- What changed in the latest run — the dashboard's first screen. Empty until there are two
-- runs with measurements, like diff.compare(store).
CREATE VIEW mart.v_changes AS
SELECT c.*
FROM (SELECT run_id FROM mart.v_runs WHERE recency = 1) now_r
CROSS JOIN LATERAL mart.changes_since(now_r.run_id) c
WHERE (SELECT count(*) FROM mart.v_runs) >= 2;
