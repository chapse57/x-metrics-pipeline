# Power BI Desktop — the same four cards

Power BI Desktop is free on Windows (Microsoft Store: "Power BI Desktop"). It has shipped its own
PostgreSQL driver (Npgsql 4.0.x) since 2019 — do **not** install Npgsql separately; a newer one
breaks the connector. What actually trips people up is TLS: the local Docker Postgres has no
certificate, and the connector tries an encrypted connection first.

## Connect

1. Home → Get data → More… → Database → **PostgreSQL database** → Connect.
2. Server `localhost:5432` · Database `xmetrics` · Data connectivity mode **Import**.
3. Credentials tab **Database** (not Windows): user `xmetrics_api`, password `xmetrics_api`.
   If it fails with an SSL / encryption message, untick "Encrypt connections" in the same dialog,
   or accept the prompt that offers to retry unencrypted. Local Docker, no certificate: expected.
4. Navigator: expand `mart`, tick `v_latest`, `v_changes`, `v_runs`. Load.

Views arrive as tables. Nothing here writes — the login cannot.

## The four visuals (one page)

| card | visual | fields |
|---|---|---|
| Accounts | Table | `v_latest`: display, tier, followers, engagement_rate, views_to_followers, days_since_last_post, measured_at. Filter: status = measured. Sort engagement_rate desc |
| Followers vs engagement | Scatter chart | X `followers` (Format → X axis → Logarithmic), Y `engagement_rate`, Legend `tier`, Details `display` |
| What changed | Table | `v_changes`: display, kind, flags, followers_prev, followers_now, followers_delta_pct, engagement_delta_pp, views_delta_pct. Conditional formatting → background color → rule: `flags` is not blank |
| Data as of | Card | measure **Age days** (below). Conditional formatting → Callout value color → rule: ≥ 8 red, else green |

`flags` comes through as text like `{views_up}` (Postgres array). Fine for a portfolio page; to
tidy it, Transform data → replace `{` and `}` with nothing.

## DAX — the minimum

```
Accounts measured = CALCULATE(COUNTROWS(v_latest), v_latest[status] = "measured")
Avg engagement %  = CALCULATE(AVERAGE(v_latest[engagement_rate]), v_latest[status] = "measured")
Changes flagged   = CALCULATE(COUNTROWS(v_changes), v_changes[flags] <> "{}")
Data taken at     = CALCULATE(MAX(v_runs[taken_at]), v_runs[recency] = 1)
Age days          = DATEDIFF([Data taken at], UTCNOW(), DAY)
```

`UTCNOW()`, not `NOW()`: `taken_at` is UTC, and mixing zones would put the red line in the wrong
place by nine hours. This is the first row of `docs/dashboard-checks.md` — write down that Power BI
displays `taken_at` in Korea time while psql and Metabase show UTC, and that they are the same instant.

## Save and commit

File → Save as → `dashboards/powerbi/x-metrics.pbix`. Screenshot the page to
`docs/powerbi-dashboard.png`. Commit both. Then fill `docs/dashboard-checks.md` from `../checks.sql`.
