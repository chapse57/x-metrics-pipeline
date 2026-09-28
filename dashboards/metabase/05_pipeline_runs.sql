-- Card 5 · Pipeline runs — did the last collect -> load -> diff finish? (mart.v_pipeline_runs)
-- Visualization: Table. Conditional formatting: row red when status = 'failed', grey when 'running'.
-- One row per attempt, newest first; a retry points at the attempt it redid (retry_of).
SELECT id,
       status,
       trigger,
       step                                   AS "failed at",
       array_to_string(steps_done, ' > ')     AS "steps done",
       run_id                                 AS "collector run",
       retry_of                               AS "retry of",
       started_at                             AS "started",
       seconds,
       alerted_at IS NOT NULL                 AS alerted,
       host,
       detail
FROM mart.v_pipeline_runs
LIMIT 10;
