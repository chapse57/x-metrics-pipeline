# ops/ — the pipeline that runs when nobody is watching

A dashboard is only worth trusting while someone can answer three questions: *did last
night's load run, did it finish, and if it did not — who knows?* This directory is those
three answers, as code.

| question | where the answer lives | how you see it |
|---|---|---|
| did it run? | `ops.pipeline_runs`, one row per attempt, written **before** the first step starts | `/health` → `last_pipeline`; Metabase card "Pipeline runs" |
| did it finish? | the row's `status`: `ok` / `failed` (+ the step that failed and the error) / `running` | same; a `running` row with no process behind it is marked failed at the next start |
| who knows? | one Slack message on failure, none on success (`ops/notify.py`) — also when the database itself cannot be reached, and for an attempt that was cut off | your phone |
| how old is the data now? | `mart.v_runs` (weeks 1–3) | gauge turns red after 8 days; `/health` says `stale: true` |
| what do I do? | the message ends with the retry command | `python -m ops.pipeline --retry <id>` |

## One command

```
python -m ops.pipeline                     # collect (docs/remeasure-20.csv) -> load -> diff
python -m ops.pipeline --trigger scheduled # same, tagged as the scheduler's run
python -m ops.pipeline --from load --run 20260923T091254Z-playwright   # collector already ran
python -m ops.pipeline --retry 12          # attempt #12 failed: redo it from the failed step
python -m ops.pipeline --fail-at load      # rehearse a failure: row + Slack message, no harm done
python -m ops.pipeline --trigger scheduled --if-due 6   # the scheduler's mode: only if the last success is 6+ days old
```

Every start does three things before any step: reach the database within 10 s
(`XMETRICS_PG_CONNECT_TIMEOUT`) or send a message that does not need it and exit 1 · take the
pipeline lock, or exit 0 if another attempt has it · close any attempt still marked `running`
(holding the lock proves its process is gone) and send one message about it.

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

`run_weekly.cmd` activates `.venv`, loads `ops\env.cmd`, starts the Postgres container and
waits up to 2 minutes for it, then runs `--if-due 6`, writing one log per start to
`out\logs\pipeline-<stamp>.log` (unbuffered, so a killed process keeps what it said). The task
has two triggers: **Sunday 22:00**, and **5 minutes after each logon**. Because of `--if-due`,
the logon start does nothing in a normal week; it collects only when the last successful run is
6+ days old — after a Sunday the PC was off, or a run a shutdown cut short. The task runs only
while you are logged on (the collector needs your browser profile).

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
did not fail · an abandoned `running` row is marked failed · an interrupted attempt is
closed and reported once · the due rule · a database that takes the connection and never
answers is given up on within the limit and still alerts · the message format · the Slack
POST · the handles file.

With `XMETRICS_PG_DSN`: the same contract against `ops.pipeline_runs` and the views · the
abandoned-row sweep in SQL · the reader can read the ledger and cannot write it · `/health`
reports the last attempt and goes `ok: false` on a failed one · the report rebuilt from SQL
equals the Python one for every run · the diff step refuses a database that disagrees with
the file · the interrupted-row sweep in SQL, at any age · a second attempt does nothing while
one holds the lock · `--if-due` skips after a recent success and releases the lock.

## The first scheduled run failed, and nothing said so (2026-10-04)

What was found on 10-08, in the order it was found:

| evidence | what it said |
|---|---|
| Task Scheduler | last run 2026-10-04 22:00, result `0xC000013A` (console closed / Ctrl+C / logoff / shutdown — not the 2 h limit) |
| `out\logs\pipeline-20261004-2200.log` | created at 22:00, **0 bytes** |
| Windows event 1074 | power off from the Start menu at **22:40:52** |
| `ops.pipeline_runs` | 4 rows, all 09-28 rehearsals — **no row for 10-04** |
| `docker ps` on 10-08 | Docker Desktop up, **no container running** |
| starting Postgres on 10-08 | "not yet accepting connections — consistent recovery state has not been yet reached" for about a minute (a hard power-off with the container running, some earlier day) |

So: after a restart Docker Desktop came up but the Postgres container did not (no restart
policy). At 22:00 the pipeline tried to connect, and the connection waited with no limit —
a normal start logs its first line within a second; this one logged nothing in 40 minutes and
wrote no row. At 22:40 the PC was switched off and the process died. No row, no log line, no
Slack message: every one of the three was downstream of the database.

Reproduced on 10-08 with the fix in place: `docker compose stop postgres`, then the pipeline.
The answer was **`connection timeout expired`**, not "connection refused": on this PC, with the
container stopped, a connection to `localhost:5432` is taken and never answered (Docker Desktop's
port forwarding), so without a limit it waits for ever. With the 10 s limit the pipeline gave up
and the message went out with no database: `docs/slack-db-unreachable.png`.

What changed, one line per hole:

| hole | now |
|---|---|
| container not up after a reboot | `restart: unless-stopped` on postgres, api, metabase; `run_weekly.cmd` also starts postgres and waits for `pg_isready` |
| connection waits forever | `connect_timeout` 10 s on every connection the pipeline opens |
| the alert needed the database | an unreachable database sends its own message, no ledger involved |
| killed process leaves nothing | log written unbuffered; the next start (holding the lock) closes the orphan `running` row and says so |
| a missed or cut-off week waits for next Sunday | logon trigger + `--if-due 6`: caught up at the next logon |
| found while fixing, never reached: the last line the pipeline prints has an em dash, which a Korean Windows (cp949) cannot write into the redirected log — a successful run would have ended in a traceback and exit 1 | `PYTHONUTF8=1` in `run_weekly.cmd` |

### The same morning: the first run through the scheduled path (10-08)

With the fixes in, `schtasks /Run` started the job for real. `--if-due 6` found the last success
was 10 days old and ran it. What followed, attempt by attempt — each one recorded, each failure
one Slack message with its retry command:

| attempt | what happened | what it showed |
|---|---|---|
| #5 scheduled | collect failed: the **headless** browser was answered with a download instead of `x.com/home`, four tries | the pipeline had never collected headless before: the 09-23 collection was headed, the 09-28 rehearsals started at load |
| #6 retry, headed | collect failed: **not logged in** | the session cookies saved by `xmetrics import-cookies` on 09-06 had stopped working, and they are re-applied on every launch |
| — | cookies imported again from the browser where X was logged in | |
| #7 retry, headed | **ok**: 20 tried, 19 measured, 1 could not be read · load · diff: 7 flagged, 0 new, 0 dropped · **SQL == Python on 19 of 19 rows** | the first run where every comparable row was compared (09-28 had 3 of 20: the baseline then lived only in the legacy file) |

Changed because of it: the pipeline is **headed by default** (`--headless` to opt out, not verified
against X), so the retry command in an alert runs in the mode that works. Known, not changed yet: a
session file saved by `import-cookies` is re-applied on every launch and overrides the browser
profile's own login, so a stale file breaks collection even after a fresh `xmetrics login`.

What still is not covered: if the PC stays off, nothing runs and nothing is sent from it —
the only signal is the dashboard's age gauge turning red after 8 days. A server, or a
check that runs somewhere else, is the fix for that; on one PC it is a known limit.

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

## Backup and restore drill — `ops/backup.py`

```
call ops\env.cmd
set XMETRICS_PG_DOCKER=postgres          # run pg_dump / pg_restore inside the compose service
python -m ops.backup drill               # dump -> restore into a fresh database -> compare -> drop the copy
```

The comparison covers every table in `raw`, `core`, `mart`, `ops`, `auth` and `pg`: row count and a
checksum over all rows (md5 of each row's text, sorted, md5 of the lot). Exit code 1 on any
difference. `tests/test_backup.py` does the same on the CI data, then changes one value in the copy
and checks the comparison names that table, and that the restored copy keeps the read-only role.
