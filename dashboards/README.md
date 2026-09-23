# dashboards/ — the same four cards, in two tools

One screen, four cards, built twice: once in Metabase (open source, runs on the client's server,
what this pipeline is delivered with) and once in Power BI Desktop (what most job posts ask for).
Both read only `mart.*` views through the read-only login, so a number on either screen is a number
`tests/test_pg.py` already proved equal to the Python report. The point of building it twice is
`checks.sql`: the two tools must show the same figures, and every difference gets written down.

| card | query | Metabase visualization | why it is on the screen |
|---|---|---|---|
| Accounts | `metabase/01_accounts.sql` | Table | the list a client scrolls |
| Followers vs engagement | `metabase/02_followers_vs_engagement.sql` | Scatter, log X | where the small-but-loud accounts are |
| What changed | `metabase/03_changes.sql` | Table, rows colored when `flagged` | the retainer deliverable, live |
| Data as of | `metabase/04_freshness.sql` | Gauge on `age days` (0–8 green, 8+ red) + Detail | says when to trust the screen — the card other dashboards lack |

## Metabase, step by step

```
docker compose up -d metabase          # first start takes about a minute (it is a Java app)
http://localhost:3000
```

1. **Setup wizard.** Any name/email/password — this is the dashboard's own admin, local to your
   machine. Skip "add your data" for now (next step does it with the right login).
2. **Add the database.** Admin (gear, top right) → Databases → Add database → PostgreSQL.
   Display name `x-metrics` · Host `postgres` (the compose service name, not localhost — Metabase
   runs inside the compose network) · Port `5432` · Database `xmetrics` · Username `xmetrics_api` ·
   Password `xmetrics_api`. Under **Schemas** choose "Only these…" and enter `mart`. Save. Metabase
   syncs and shows `v_latest`, `v_changes`, `v_freshness`, `v_runs`, `v_run_status`.
3. **Four questions.** For each file in `metabase/`: New → SQL query → pick `x-metrics` → paste →
   run → choose the visualization named in the file's header comment → Save, name it as in the
   table above. `04_freshness.sql` is saved twice: once as a Gauge (set ranges 0–8 green, 8–30 red
   in the gauge settings), once as Detail. The gauge reads the *first* column of the single row,
   which is why `age days` comes first in that file — with `taken_at` first Metabase refuses
   ("gauge needs a number").
4. **One dashboard.** New → Dashboard → `x-metrics` → add the five saved questions. Layout used
   (24-column grid, **fixed width**): gauge 5 wide + detail 7 wide + scatter 12 wide on the top row,
   accounts table full width below, changes table full width at the bottom. Full width stretches
   the cards until the page no longer fits one screen — fixed width keeps it to one screenshot.
   Each question carries its one-line description (the ⓘ on the card); the dashboard description
   carries the sentence the client gets: *"This dashboard never reports an account as gone unless we
   actually went looking for it and it wasn't there. If we didn't look, it says so."*
   Before the screenshot, switch Metabase to English (Admin → Settings → Localization, and your
   account's language): otherwise dates render as "9월 23, 2026, 9:17 오전".
5. **Screenshot** the dashboard (Win+Shift+S, dashboard area only) to `docs/metabase-dashboard.png`.
   That is portfolio slide one.
6. **Public link** (so a proposal can say "here is the URL"): Admin → Settings → Public sharing →
   enable; then on the dashboard, share icon → Public link. Note the link in `docs/` only if you
   would actually send it — a public link is public.

Metabase stores its questions in its own H2 file inside the `metabase_app` volume. `docker compose
down` keeps it; `docker compose down -v` deletes it along with the Postgres volume.

## Power BI Desktop

See `powerbi/README.md`. Same four cards, same views, `.pbix` committed next to it.

## After both are built: `checks.sql`

Run it (`docker compose exec postgres psql -U xmetrics_api -d xmetrics -P pager=off -f /dev/stdin < dashboards/checks.sql`),
then read the same nine figures off each dashboard and fill `docs/dashboard-checks.md`:

| figure | psql | Metabase | Power BI | same? |
|---|---|---|---|---|

Two differences are expected and are conventions, not bugs: **time zone** (psql and Metabase show
UTC because the API and Metabase run in UTC; Power BI Desktop shows your Windows zone, +9 h) and
**rounding on display** (a tool may show 0.16 for 0.1600). Anything else is a bug: find it before
the screenshot.
