# pg/ — the same data on PostgreSQL

The collector writes SQLite. This layer copies it into PostgreSQL so that something other than
a Python script can read it: a dashboard, an API, another team's tool. Three schemas:

| schema | what | rule |
|---|---|---|
| `raw` | the SQLite tables, column for column, plus `source_db` and `loaded_at` | never corrected by hand |
| `core` | the same rows with proper types and **one** documented correction | the only place a value may differ from raw |
| `mart` | what a dashboard reads: `v_latest`, `v_changes`, `v_freshness`, `v_runs` | views only, nothing stored |

```
docker compose -f pg/docker-compose.yml up -d
export XMETRICS_PG_DSN=postgresql://xmetrics:xmetrics@localhost:5432/xmetrics
python -m pg.migrate                                   # idempotent; a ledger in pg.schema_migrations
python -m pg.load out/x_legacy_claude.db out/x.db      # idempotent; ON CONFLICT DO UPDATE on the SQLite keys
python -m pg.load --run 20260918T020745Z-playwright out/x.db    # one run, for re-processing
python -m pytest tests/test_pg.py -q                   # 9 tests; skipped when the DSN is not set
```

## What the tests prove

`tests/test_pg.py` — migration twice adds no object · load counts equal SQLite counts · loading
everything again, in either order, changes no row · `--run` touches one run · and the one this
layer exists for:

**`mart.changes(run_prev, run_now)` returns exactly what `xmetrics.diff.compare()` returns** — same
rows, same order, same flags, same rounded deltas — for every consecutive pair of runs in
`out/x.db`, at default thresholds and at tightened ones. The SQL function is a line-for-line port
of `compare_rows`; the test is what keeps it one.

## Two things found while making that test pass

**Rounding.** Python's `round()` is half-to-even on the binary value; PostgreSQL's
`round(numeric)` is half away from zero. `diff._round` now follows PostgreSQL (see the README's
"What changed" section for why). The SQL side has a trap of its own: `float8::numeric` formats
with 15 significant digits, so `28.124999999999996` becomes `28.125` and rounds *up*, while
Python's `repr()` keeps `28.124999999999996` and rounds *down*. `mart.round_half_up` therefore
goes `float8 → text → numeric`; `float8::text` is shortest-round-trip, the same digits `repr()`
produces, and the two sides agree on all 308 values the test throws at them.

**Legacy dates.** The 2026-09 legacy import stamped every measurement with the *import* time
(`2026-09-07T02:24`), not the measurement time. `core.measurements` re-dates those rows to the
end of their measurement window (`range_end`, 2026-08-25 … 09-03) and keeps the original as
`recorded_at`; `raw` is untouched. Without this, `v_freshness` would call two-week-old data
"from September 7".

## Known limit: partial runs

`v_changes` compares the two most recent runs *as sets of accounts*, exactly as `diff.py` does.
After loading the legacy baseline (95 accounts) and the 2026-09-18 live run (3 accounts), the
view reports 3 changed and **92 dropped** — correct by the definition, useless on a dashboard.
The definition assumes every run measures the whole list. Re-measuring 20 accounts next week
will show 75 "dropped" the same way. Options, not yet decided: compare each account's last two
measurements regardless of run (loses `new`/`dropped`), or mark runs as full or partial and only
diff full ones against each other. `diff.py` and `mart.changes` must change together.
