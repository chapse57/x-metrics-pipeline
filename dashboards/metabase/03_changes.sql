-- Card 3 · What changed in the latest run (mart.v_changes — the report tests/test_pg.py proves
-- equal to diff.py). Visualization: Table. Conditional formatting: color the row when
-- "flagged" is true; "dropped" rows first because the view already sorts them first.
-- Reminder for the card description: "dropped" means we went looking and the account was not
-- there. Accounts this run did not try are not listed.
SELECT display                                   AS account,
       kind,
       array_to_string(flags, ', ')              AS flags,
       cardinality(flags) > 0                    AS flagged,
       followers_prev                            AS "followers before",
       followers_now                             AS "followers now",
       followers_delta_pct                       AS "followers Δ%",
       engagement_delta_pp                       AS "engagement Δpp",
       views_delta_pct                           AS "views Δ%",
       days_since_last_post_prev                 AS "days silent before",
       days_since_last_post_now                  AS "days silent now",
       run_prev                                  AS "compared with run"
FROM mart.v_changes;
