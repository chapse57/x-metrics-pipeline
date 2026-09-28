# ops/ — the pipeline that runs when nobody is watching

A dashboard is only worth trusting while someone can answer three questions: *did last
night's load run, did it finish, and if it did not — who knows?* This directory is those
three answers, as code.

| question | where the answer lives | how you see it |
|---|---|---|
| did it run? | `ops.pipeline_runs`, one row per attempt, written **before** the first step starts | `/health` → `last_pipeline`; Metabase card "Pipeline runs" |
| did it finish? | the row's `status`: `ok` / `failed` (+ the step that failed and the error) / `running` | same; a `running` row older than 6 h is marked failed at the next start |
| who knows? | one Slack message on failure, none on success (`ops/notify.py`) | your phone |
| how old is the data now? | `mart.v_runs` (weeks 1–3) | gauge turns red after 8 days; `/health` says `stale: true` |
| what do I do? | the message ends with the retry command | `python -m ops.pipeline --retry <id>` |

## One command

```
python -m ops.pipeline                     # collect (docs/remeasure-20.csv) -> load -> diff
python -m ops.pipeline --trigger scheduled # same, tagged as the scheduler's run
python -m ops.pipeline --from load --run 20260923T091254Z-playwright   # collector already ran
python -m ops.pipeline --retry 12          # attempt #12 failed: redo it from the failed step
python -m ops.pipeline --fail-at load      # rehearse a failure: row + Slack message, no harm done
```

Needs `XMETRICS_PG_DSN` (the **owner** login — this is the one process that writes) and,
for collect, the browser profile from `xmetrics login`. `XMETRICS_SLACK_WEBHOOK` is optional:
without it, failures are logged instead of sent.

Exit code 0 = ok, 1 = failed, so Task Scheduler / cron see the result too.

### The three steps

1. **collect** — `xmetrics.collect.Collector` on the handles in `--handles-file` (default
   `docs/remeasure-20.csv`, the 20 accounts chosen in week 1) or `--all`. The run declares
   its targets before it starts (`run_targets`); if it ends with any target still `pending`,
   the step fails: a half-finished collection is not a collection.
2. **load** — `pg.load.load(conn, db, run_id=...)`: only this run's rows. Upserts, so loading
   the same run twice changes nothing — which is what makes a retry safe.
3. **diff** — the report is read **from the database** (`pg/report.py`,
   `mart.changes_since(run)`) and written to `out/changes.md` + `changes.json`. Then the same
   report is computed in Python over the local SQLite file, and the step fails if the two
   disagree on any account both sides can see. That is the week-1 CI assertion, repeated on
   every scheduled run, on real data.

   Why from the database and not from the file: the file on the collecting PC holds the runs
   that PC made; the database holds every run from every file, including the hand-measured
   baseline. The database is the whole history, so the weekly report comes from it.

## The ledger (`pg/schema/006_ops.sql`)

```
ops.pipeline_runs
  id · started_at · finished_at · status (running|ok|failed) · trigger (scheduled|manual|retry)
  host · step · steps_done[] · run_id · retry_of · detail · alerted_at
mart.v_pipeline_runs    every attempt, newest first, with `seconds`
mart.v_pipeline_latest  the newest one — what /health reports
```

Rules the table enforces: a `running` row has no `finished_at`, a finished row has one; a
retry is a **new** row pointing at the old one (`retry_of`), so a failure is never
overwritten by the retry that fixed it. The reader role sees it through `mart.*`; only the
owner writes.

`/health.ok` is now false for any of: database down · data older than 8 days · last attempt
failed. The dashboard's age gauge covers the second; the "Pipeline runs" card the third.

## Schedule it

**Windows (the PC with the logged-in browser profile):**

```
copy ops\env.example.cmd ops\env.cmd          # then fill in the DSN and the webhook
powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1        # Sunday 22:00
schtasks /Run /TN "x-metrics weekly"                                     # try it now
```

`run_weekly.cmd` activates `.venv`, loads `ops\env.cmd`, and writes one log per run to
`out\logs\pipeline-<stamp>.log`. The task runs only while you are logged on (the collector
needs your browser profile) and catches up if the PC was asleep at 22:00.

**Linux / macOS:**

```
0 22 * * 0  cd /path/to/x-metrics-pipeline && XMETRICS_PG_DSN=... XMETRICS_SLACK_WEBHOOK=... \
            .venv/bin/python -m ops.pipeline --trigger scheduled >> out/logs/pipeline.log 2>&1
```

Not in `docker-compose.yml` on purpose: the collector needs a browser logged in as a person,
and that belongs on a person's machine, not in a container.

## Slack

Create a free workspace → *Apps* → *Incoming Webhooks* → add to a channel → copy the URL into
`XMETRICS_SLACK_WEBHOOK`. Prove it before you rely on it:

```
python -m ops.notify "x-metrics: webhook works"
python -m ops.pipeline --fail-at diff        # the real message, from a rehearsed failure
```

The message names the attempt, the step, the first line of the error, the host, and the
retry command — everything needed to act, nothing that needs a login to read.

## Tests (`tests/test_ops.py`)

Without a server: an ok attempt records every step and sends nothing · a failure names the
step, keeps the steps that passed, records the collector run, alerts exactly once · an alert
that could not be sent is not marked sent · retry starts at the failed step with the same run
· a retry of a failed collect collects again under a new run · retry refuses an attempt that
did not fail · an abandoned `running` row is marked failed · the message format · the Slack
POST · the handles file.

With `XMETRICS_PG_DSN`: the same contract against `ops.pipeline_runs` and the views · the
abandoned-row sweep in SQL · the reader can read the ledger and cannot write it · `/health`
reports the last attempt and goes `ok: false` on a failed one · the report rebuilt from SQL
equals the Python one for every run · the diff step refuses a database that disagrees with
the file.

## Metabase's own database (seen 2026-09-28)

Metabase keeps its dashboards and cards in a database of its own. In `docker-compose.yml` that
is H2, a single file in the `metabase_app` volume — fine on a laptop, not on a client server.
After `docker compose up -d --build` restarted the container, H2 had lost the tail of its ID
sequence (it hands out IDs in blocks and an unclean stop forgets the block): saving a new card
failed with `PRIMARY KEY ON PUBLIC.REPORT_CARD(ID)` until the sequence had walked past the
existing cards — eight Save clicks. Nothing in *our* Postgres was involved.

On a client server, point Metabase's application database at Postgres too
(`MB_DB_TYPE=postgres`, `MB_DB_HOST/PORT/DBNAME/USER/PASS`), in a database separate from the
data it reports on. Not switched here on purpose: switching wipes the H2 dashboards unless they
are migrated first (`load-from-h2`), and this repo's dashboard is rebuilt from
`dashboards/metabase/*.sql` in minutes anyway.
