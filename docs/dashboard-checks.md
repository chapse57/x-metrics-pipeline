# Dashboard checks — the same nine numbers in three tools (2026-10-08)

The claim this repo makes is that the dashboard shows the database's numbers, not its own. This
is that claim checked once, by hand, on real data: the nine figures in
[`dashboards/checks.sql`](../dashboards/checks.sql), read from PostgreSQL, from the Metabase
dashboard and from the Power BI report, all three looking at the same data.

**Data state:** latest run `20261008T023253Z-playwright` (attempt #7, 20 accounts tried, 19
measured, 1 could not be read), loaded 2026-10-08 02:37 UTC. Power BI refreshed just before reading.

**How each tool was read, and why it counts as a check:**

| tool | how | what computes the number |
|---|---|---|
| psql | `checks.sql` as written | PostgreSQL, the reference |
| Metabase | each dashboard card → *Explore results* → *Summarize* (count, average, max, group by) | Metabase's own query builder, over the card's result |
| Power BI | DAX query view, one `EVALUATE ROW(...)` over the imported model | Power BI's engine (DAX) over its own copy of the data — the import, not the database |

## The nine figures

| # | figure | psql | Metabase | Power BI | same? |
|---|---|---|---|---|---|
| 1 | accounts measured | 94 | 94 | 94 | yes |
| 2 | average engagement % (measured) | 0.1555 | 0.16 | 0.155468 | yes — Metabase shows 2 decimals; 0.155468 rounds to 0.1555 |
| 3 | max followers | 2,500,000 | 2,500,000 | 2500000 | yes |
| 4 | change rows (latest run) | 19 | 19 | 19 | yes |
| 5 | flagged | 7 | 7 (`flagged` true) | 7 | yes |
| 6 | dropped | 0 | 0 (only `kind` = changed) | *(blank)* | yes — DAX returns BLANK, not 0, when no row matches |
| 7 | latest run | `20261008T023253Z-playwright` | `20261008T023…` (card width) | `20261008T023253Z-pla…` (column width) | yes |
| 8 | data taken at | 2026-10-08 02:37:08 UTC | October 8, 2026, 2:37 AM | 10/8/2026 11:37:08 AM | yes — Power BI shows Windows local time (KST, +9 h) |
| 9 | age in days | 0 | 0 (gauge) | 0 (Age days card) | yes |

Nine of nine agree. The only differences are the two conventions written down before the check
(time zone, rounding on display) and one more found during it (DAX blank for an empty count).

## What the check turned up anyway

- **94, not 95.** One account (@brianleetrades) was reached but could not be read in this run. Its
  status is now `error`, so every "measured" filter drops it — in all three tools alike. The rule
  is right (an unread account is not a measurement), and the count moving is how you see it.
- **A title that lied.** The Power BI table was titled "Accounts (latest measurement, 95 accounts)":
  a number typed into a title cannot follow the data. Changed to "Accounts (latest measurement,
  measured only)". Metabase's title had no number.
- **Two ways to say "flagged", same answer.** SQL counts rows whose `flags` array is non-empty;
  Power BI imports the array as text and counts rows whose text is not `{}`. Different logic,
  same 7 — but if the import format of arrays ever changes, this is the figure that would break.
- **A stale import shows itself.** Before the refresh, Power BI's Age days card read **14, in red**
  — the report was carrying the 09-23 data. The freshness card did its job in the one tool that
  keeps its own copy.

## Repeat it

```
docker compose exec -T postgres psql -U xmetrics -d xmetrics -x -P pager=off < dashboards\checks.sql
```

Power BI: *Home → Refresh*, then the DAX query view:

```
EVALUATE
ROW(
  "1 accounts_measured", CALCULATE(COUNTROWS('mart v_latest'), 'mart v_latest'[status] = "measured"),
  "2 avg_engagement_pct", FORMAT(CALCULATE(AVERAGE('mart v_latest'[engagement_rate]), 'mart v_latest'[status] = "measured"), "0.000000"),
  "3 max_followers", CALCULATE(MAX('mart v_latest'[followers]), 'mart v_latest'[status] = "measured"),
  "4 changes_rows", COUNTROWS('mart v_changes'),
  "5 changes_flagged", CALCULATE(COUNTROWS('mart v_changes'), 'mart v_changes'[flags] <> "{}"),
  "6 changes_dropped", CALCULATE(COUNTROWS('mart v_changes'), 'mart v_changes'[kind] = "dropped"),
  "7 latest_run", CALCULATE(MAX('mart v_runs'[run_id]), 'mart v_runs'[recency] = 1),
  "8 data_taken_at", CALCULATE(MAX('mart v_runs'[taken_at]), 'mart v_runs'[recency] = 1),
  "9 age_days", [Age days]
)
```

This was done by hand, once. It is not in CI: Metabase and Power BI are not in the test run.
What CI does check, on every push, is the step before the dashboards — that the SQL the
dashboards read equals the Python computation (`tests/test_pg.py`).
