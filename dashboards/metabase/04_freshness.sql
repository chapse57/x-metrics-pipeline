-- Card 4 · Data as of — the card no other dashboard has (mart.v_runs, latest run).
-- Two questions from this one query:
--   (a) Visualization: Gauge on "age days". Ranges: 0–8 green, 8–30 red. The 8-day line is the
--       same rule /health uses (stale_after_days) — the card turns red on the day the API says stale.
--   (b) Visualization: Detail / Table showing the rest: when the numbers were taken, which run,
--       how many accounts it set out to measure, how many it reached, whether it was complete.
SELECT taken_at                                                    AS "data taken at",
       floor(EXTRACT(EPOCH FROM now() - taken_at) / 86400)::int    AS "age days",
       (EXTRACT(EPOCH FROM now() - taken_at) / 86400) > 8          AS stale,
       run_id,
       targets                                                     AS "accounts tried",
       accounts_measured                                           AS "accounts measured",
       not_reached                                                 AS "not reached",
       complete
FROM mart.v_runs
WHERE recency = 1;
