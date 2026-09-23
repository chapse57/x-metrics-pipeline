-- Card 1 · Accounts — every tracked account's latest measurement (mart.v_latest).
-- Visualization: Table. Sort is baked in; leave column order as returned.
SELECT display                       AS account,
       tier,
       followers,
       engagement_rate               AS "engagement %",
       views_to_followers            AS "views / followers %",
       days_since_last_post          AS "days since last post",
       measured_at                   AS "measured at",
       source
FROM mart.v_latest
WHERE status = 'measured'
ORDER BY engagement_rate DESC, handle COLLATE "C";
