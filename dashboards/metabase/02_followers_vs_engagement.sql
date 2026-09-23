-- Card 2 · Followers vs engagement — one dot per account (mart.v_latest).
-- Visualization: Scatter. X = followers (set the axis to log scale: 3K and 2.5M on one chart),
-- Y = "engagement %", bubble label / detail = account, color by tier.
SELECT display              AS account,
       tier,
       followers,
       engagement_rate      AS "engagement %"
FROM mart.v_latest
WHERE status = 'measured';
