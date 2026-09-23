-- The numbers both dashboards must agree on. Run this in psql, then read the same figures off
-- Metabase and Power BI. Any difference is either a bug to find or a display convention to
-- write down (time zone, rounding) — never something to leave unexplained.
--
--   docker compose exec postgres psql -U xmetrics_api -d xmetrics -P pager=off -f /dev/stdin < dashboards/checks.sql
--   (or paste it into a Metabase native question)
SELECT
  (SELECT count(*)                          FROM mart.v_latest WHERE status = 'measured')  AS accounts_measured,
  (SELECT round(avg(engagement_rate)::numeric, 4) FROM mart.v_latest WHERE status = 'measured') AS avg_engagement_pct,
  (SELECT max(followers)                    FROM mart.v_latest WHERE status = 'measured')  AS max_followers,
  (SELECT count(*)                          FROM mart.v_changes)                          AS changes_rows,
  (SELECT count(*)                          FROM mart.v_changes WHERE cardinality(flags) > 0) AS changes_flagged,
  (SELECT count(*)                          FROM mart.v_changes WHERE kind = 'dropped')   AS changes_dropped,
  (SELECT run_id                            FROM mart.v_runs WHERE recency = 1)           AS latest_run,
  (SELECT taken_at                          FROM mart.v_runs WHERE recency = 1)           AS data_taken_at_utc,
  (SELECT floor(EXTRACT(EPOCH FROM now() - taken_at) / 86400)::int FROM mart.v_runs WHERE recency = 1) AS age_days;
