-- 008: the two things the MCP server could answer when it read the SQLite file, and lost when it
-- moved to Postgres on 2026-10-02 — now from Postgres, with the same answers.
--
-- 1. raw.agent_audit: every LLM classification attempt and which guardrails failed, copied from
--    each SQLite file by pg/load.py. Keyed by (source_db, id): ids restart in every file.
-- 2. mart.v_validation_issues: the checks xmetrics/validate.py runs before an export
--    (recompute, rules, duplicates), written as SQL over raw rows. tests/test_pg.py asserts
--    that SQL and Python list the same issues — the same "computed twice, asserted equal" rule
--    as the change report.

CREATE TABLE IF NOT EXISTS raw.agent_audit (
  source_db     text NOT NULL,
  id            integer NOT NULL,
  handle        text NOT NULL,
  at            timestamptz NOT NULL,
  classifier    text NOT NULL,               -- 'claude:<model>' | 'rule'
  raw_output    text,                        -- the model's output, untouched
  parsed        text,                        -- JSON after parsing, as stored (may be NULL)
  verdict       text NOT NULL CHECK (verdict IN ('accepted', 'review', 'rejected')),
  failed_checks jsonb NOT NULL DEFAULT '[]', -- guardrail names that failed
  loaded_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_db, id)
);

-- What Store.agent_audit_summary() returns, one view per part.
CREATE VIEW mart.v_agent_audit_verdicts AS
SELECT verdict, count(*)::int AS n FROM raw.agent_audit GROUP BY verdict;

CREATE VIEW mart.v_agent_audit_checks AS
SELECT c AS guardrail, count(*)::int AS n
FROM raw.agent_audit, jsonb_array_elements_text(failed_checks) AS c
GROUP BY c;

CREATE VIEW mart.v_agent_audit_classifiers AS
SELECT classifier, count(*)::int AS n FROM raw.agent_audit GROUP BY classifier;

-- The rows validate.py checks: Store.latest_measurements() — per account with status 'measured',
-- the measurement with the latest measured_at as stored (for legacy rows that is the import
-- time, which is what the SQLite file holds; core.measurements' range_end correction is for
-- freshness, not for this).
CREATE VIEW core.validation_rows AS
SELECT DISTINCT ON (m.handle)
       m.*, a.dm_open, a.bio_url, a.niche
FROM raw.measurements m
JOIN raw.accounts a ON a.handle = m.handle
WHERE a.status = 'measured'
ORDER BY m.handle, m.measured_at DESC;

-- Same thresholds as validate.DEFAULT_RULES and the 0.006 tolerance in check_recompute.
CREATE VIEW mart.v_validation_issues AS
WITH r AS (SELECT * FROM core.validation_rows),
own AS (   -- pipeline-collected rows: medians recomputed from the raw posts
  SELECT p.handle, p.run_id, count(*) AS n,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(p.likes, 0) + coalesce(p.replies, 0) + coalesce(p.reposts, 0)) AS m_int
  FROM raw.posts p
  WHERE coalesce(p.is_repost, 0) = 0 AND coalesce(p.is_pinned, 0) = 0
  GROUP BY p.handle, p.run_id
),
has_posts AS (SELECT DISTINCT handle, run_id FROM raw.posts),
calc AS (
  SELECT r.*, (hp.handle IS NOT NULL) AS from_posts, coalesce(o.n, 0) AS own_n,
         CASE WHEN o.n IS NULL THEN 0 ELSE round((o.m_int / r.followers * 100)::numeric, 4)::float8 END AS er_posts,
         CASE WHEN r.followers = 0 THEN 0
              ELSE round(((r.median_likes + r.median_replies + r.median_reposts) / r.followers * 100)::numeric, 4)::float8 END AS er_medians,
         CASE WHEN r.followers = 0 THEN 0 ELSE round((r.median_views / r.followers * 100)::numeric, 4)::float8 END AS vr,
         CASE WHEN r.followers < 25000 THEN 'Micro (<25K)' WHEN r.followers < 100000 THEN 'Mid (25-100K)' ELSE 'Macro (100K+)' END AS tier_calc
  FROM r
  LEFT JOIN has_posts hp ON hp.handle = r.handle AND hp.run_id = r.run_id
  LEFT JOIN own o ON o.handle = r.handle AND o.run_id = r.run_id
),
urls AS (
  SELECT lower(btrim(bio_url)) AS u, count(*) AS n FROM r WHERE btrim(coalesce(bio_url, '')) <> '' GROUP BY 1
)
SELECT handle, 'recompute.posts_measured' AS "check", 'error' AS severity,
       format('stored %s vs raw %s', posts_measured, own_n) AS detail
FROM calc WHERE from_posts AND own_n <> posts_measured
UNION ALL
SELECT handle, 'recompute.engagement_rate', 'error', format('stored %s vs raw %s', engagement_rate, er_posts)
FROM calc WHERE from_posts AND abs(er_posts - engagement_rate) > 0.006
UNION ALL
SELECT handle, 'recompute.engagement_rate', 'error', format('stored %s vs recomputed %s', engagement_rate, er_medians)
FROM calc WHERE NOT from_posts AND abs(er_medians - engagement_rate) > 0.006
UNION ALL
SELECT handle, 'recompute.views_to_followers', 'error', format('stored %s vs recomputed %s', views_to_followers, vr)
FROM calc WHERE abs(vr - views_to_followers) > 0.06
UNION ALL
SELECT handle, 'recompute.tier', 'error', format('stored %s vs %s', tier, tier_calc)
FROM calc WHERE tier <> tier_calc
UNION ALL
SELECT handle, 'rules.min_posts', 'error', format('%s < 10', posts_measured) FROM r WHERE posts_measured < 10
UNION ALL
SELECT handle, 'rules.min_followers', 'warn', format('%s < 1000', followers) FROM r WHERE followers < 1000
UNION ALL
SELECT handle, 'rules.recency', 'warn', format('last post %sd ago (> 21)', days_since_last_post)
FROM r WHERE days_since_last_post > 21
UNION ALL
SELECT handle, 'rules.contact', 'error', 'no DM and no bio URL'
FROM r WHERE coalesce(dm_open, 0) = 0 AND btrim(coalesce(bio_url, '')) = ''
UNION ALL
SELECT handle, 'rules.niche', 'warn', 'niche missing — run `classify` (delivered flagged)'
FROM r WHERE btrim(coalesce(niche, '')) = ''
UNION ALL
SELECT r.handle, 'dup.bio_url', 'warn', format('bio URL shared by %s accounts: %s', urls.n, urls.u)
FROM r JOIN urls ON urls.u = lower(btrim(r.bio_url)) WHERE urls.n > 1;
