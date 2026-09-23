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
python -m pytest tests/test_pg.py -q                   # 12 tests; skipped when the DSN is not set
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

## Run targets, or what "dropped" means

`raw.run_targets` holds one row per account a run set out to measure and what became of it
(`pending` · `measured` · `missing` · `error`). `mart.changes_since(run)` compares every account
the run measured with that account's own previous measurement, and reports "dropped" only for
targets that came back `missing` and had something to lose. `mart.v_run_status` counts the four
outcomes per run and says whether the run was complete; `mart.v_runs` orders runs by when their
numbers were taken. The reasoning, and the history of the summary column this replaced, is at the
top of `schema/004_run_targets.sql` and in the main README's "What changed" section.

On the real data — a 95-account import, then a 3-account live run — `mart.v_changes` reports
3 changed and 0 dropped, by data, not by hand: the live run recorded 3 targets, so it says nothing
about the other 92.

## Migrations are applied once

`pg/migrate.py` keeps a ledger (`pg.schema_migrations`) and skips files already in it, so a file
that has been applied anywhere is never edited — a change is a new numbered file, which is why
`004` drops and recreates what `002` made rather than editing `002`. `tests/test_pg.py` asserts
that a second `migrate` applies nothing and leaves every object as it was; it does not, and need
not, prove that any single file could be re-run by hand.
