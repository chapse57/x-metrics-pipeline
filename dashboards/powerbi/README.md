# Power BI Desktop — the same four cards

Power BI Desktop is free on Windows (Microsoft Store: "Power BI Desktop"). It has shipped its own
PostgreSQL driver (Npgsql 4.0.x) since 2019 — do **not** install Npgsql separately; a newer one
breaks the connector. What actually trips people up is TLS: the local Docker Postgres has no
certificate, and the connector tries an encrypted connection first.

## Connect

1. Home → Get data → More… → Database → **PostgreSQL database** → Connect.
2. Server `localhost:5432` · Database `xmetrics` · Data connectivity mode **Import**.
3. Credentials tab **Database** (not Windows): user `xmetrics_api`, password `xmetrics_api`.
   Power BI then says it cannot connect with encryption and offers an unencrypted connection
   (Korean UI: "암호화 지원" → 확인). Accept it for the local Docker database, which has no
   certificate. Against a client's server, do not: ask for the server's certificate or an SSL-enabled
   endpoint instead.
4. Navigator: expand `mart`, tick `v_latest`, `v_changes`, `v_runs`. Load.

Views arrive as tables. Nothing here writes — the login cannot.

## The four visuals (one page) — as built in `x-metrics.pbix`

| card | visual | fields |
|---|---|---|
| Data as of (days since the latest run; red after 8) | Card | measure **Age days** (below). Format → Callout value → Color → fx → Rules: `>= 0 and < 8` green `#2E8B57`, `>= 8 and < 100000` red |
| Followers vs engagement | Scatter chart | Values `display`, X `followers`, Y `engagement_rate`, Legend `tier`. Format → X axis → Range → **Logarithmic scale on**; X axis → Values → **Display units: None** |
| Accounts (latest measurement, 95 accounts) | Table | `mart v_latest`: display, tier, followers, engagement_rate, views_to_followers, days_since_last_post. Filter on this visual: `status` = measured (95). Totals off |
| What changed (latest run vs each account's previous measurement) | Table | `mart v_changes`: display, kind, flags, followers_prev, followers_now, followers_delta_pct, engagement_delta_pp, views_delta_pct (20 rows) |

Power BI names the imported views `mart v_latest`, `mart v_changes`, `mart v_runs` (schema, space,
view). Numeric fields are summarized by default, so a column arrives as "Sum of followers" — each
table has one row per account, so the sum *is* the value, but the header is wrong. Every field in
the four visuals is renamed (double-click it in the field well) to the same name Metabase shows:
`account`, `followers`, `engagement %`, `views / followers %`, `days since last post`,
`followers before`, `followers now`, `followers Δ%`, `engagement Δpp`, `views Δ%`.

`flags` comes through as text like `{views_up}` (Postgres array). Left as is.

## Language: a client-facing file is in English

Power BI Desktop running in Korean writes Korean into the report itself, not just its menus:
"합계 … 개" (Sum of …) in field names, "1천 / 10천" on axes, and a generated chart title. A client
opening the .pbix or a screenshot sees that. Three fixes, all in the file:

1. File → Options and settings → Options → **Current file → Regional settings** → locale for
   date and number formats: **English (United States)**.
2. Rename every field in the visuals (above). This removes "Sum of …".
3. Axis labels: set **Display units: None** (the "천/K" unit label comes from the UI language, not
   the file locale). Give every visual an explicit title (Format → General → Title → Text).

Metabase has the same trap: its dates rendered as "9월 23, 2026, 9:17 오전" until the site and admin
locale were switched to English (Admin → Settings → Localization, and the account's own language).

## DAX — the one measure the page needs

```
Age days = INT(NOW() - CALCULATE(MAX('mart v_runs'[taken_at]), 'mart v_runs'[recency] = 1))
```

Why this and not `DATEDIFF(..., UTCNOW(), DAY)` (what this file said before the report was built):

- **`NOW()`, not `UTCNOW()`.** Postgres stores `taken_at` as UTC, but the PostgreSQL connector
  converts `timestamptz` to a local date/time on import: the run taken at 09:12 UTC appears as
  6:12 PM in Power BI (Korea, UTC+9). Both sides of the subtraction must be in the same zone, and
  after import that zone is local. `UTCNOW()` here would be nine hours off.
- **`INT(now - taken)`, not `DATEDIFF(…, DAY)`.** `DATEDIFF` counts calendar-day boundaries
  crossed: taken 23:50, viewed 00:10 the next day → 1. The SQL side
  (`floor(EXTRACT(EPOCH FROM now() - taken_at) / 86400)`, in `metabase/04_freshness.sql` and the
  API's stale rule) counts whole 24-hour periods → 0. Subtracting two datetimes in DAX gives days
  as a decimal; `INT` floors it. Same rule, same number, in all three places.

## Save and commit

Saved as `dashboards/powerbi/x-metrics.pbix`; the page is `docs/powerbi-dashboard.png`.
The figures both tools must agree on are in `../checks.sql`; the comparison goes in
`docs/dashboard-checks.md`.
