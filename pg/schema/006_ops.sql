-- 006_ops.sql — the operations layer: one row per pipeline attempt.
--
-- A dashboard is trusted for exactly as long as someone can answer "did last night's load
-- run, and did it finish?". raw/core/mart say what the data is; ops says what happened to
-- the process that produced it. ops/pipeline.py writes here as the owner; the API and the
-- dashboard read it through mart.v_pipeline_* as the reader.
--
-- One attempt = one row. A retry is a new row that points at the attempt it retries
-- (retry_of), so the history is never rewritten: a failure stays a failure, and the retry
-- that fixed it is a second line under it.

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE ops.pipeline_runs (
  id          bigserial PRIMARY KEY,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status      text NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
  trigger     text NOT NULL CHECK (trigger IN ('scheduled', 'manual', 'retry')),
  host        text,
  step        text,                          -- the step in progress, or the one that failed
  steps_done  text[] NOT NULL DEFAULT '{}',  -- in order: collect, load, diff
  run_id      text,                          -- the collector run this attempt produced or processed
  retry_of    bigint REFERENCES ops.pipeline_runs (id),
  detail      text,                          -- ok: one-line summary · failed: the error, first lines
  alerted_at  timestamptz,                   -- when the failure message went out (NULL = it did not)
  CHECK (status <> 'running' OR finished_at IS NULL),
  CHECK (status =  'running' OR finished_at IS NOT NULL)
);

CREATE INDEX idx_ops_pipeline_runs_started ON ops.pipeline_runs (started_at DESC);

-- The reader (API, dashboards) sees ops through mart, read-only, like everything else.
GRANT USAGE ON SCHEMA ops TO xmetrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ops TO xmetrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT SELECT ON TABLES TO xmetrics_reader;

-- Every attempt, newest first. `seconds` keeps counting while a run is still 'running'.
CREATE VIEW mart.v_pipeline_runs AS
SELECT id, started_at, finished_at, status, trigger, host, step, steps_done, run_id, retry_of,
       detail, alerted_at,
       EXTRACT(EPOCH FROM (coalesce(finished_at, now()) - started_at))::int AS seconds
FROM ops.pipeline_runs
ORDER BY started_at DESC, id DESC;

-- The newest attempt — what /health reports and the dashboard card shows.
CREATE VIEW mart.v_pipeline_latest AS
SELECT * FROM mart.v_pipeline_runs LIMIT 1;
