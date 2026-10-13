# airflow/ — local Airflow on a copy of the database, with S3 + Athena

Post-processing orchestration only, on the copy database `xmetrics_af`. Collection stays with the
Windows scheduled task: it needs the logged-in browser on the desktop, which a Linux container
cannot drive. Nothing here writes to the production database `xmetrics`.

| | |
|---|---|
| Airflow | 3.3.2, LocalExecutor (official compose cut down: no Celery, Redis, Flower, triggerer) |
| Airflow's own records | its own Postgres (`airflow-db`, no port published) |
| Data it touches | `xmetrics_af` only — `dags/_xm.py` refuses any other database name. `media_lake_glue` also **reads** the `discovery` database (media-discovery's podcast table) |
| Lake | `s3://ether-xmetrics-lake/xmetrics/<table>/dt=YYYY-MM-DD/` (ap-northeast-2), Athena database `xmetrics_lake` |
| AWS identity | IAM user `xmetrics-pipeline`, policy `../aws/iam-policy-xmetrics-lake.json` (one bucket, Athena `primary`, catalog for one database) |
| UI | http://localhost:8080 (airflow / airflow, laptop only) |

## DAGs

`xmetrics_postprocess` — one run = one week (Mondays 00:00 UTC close the week before).

    snapshot -> load -> diff ┐
                        └-> checks -> export_s3 -> athena_tables -> athena_checks

| task | what it calls | fails when |
|---|---|---|
| snapshot | copies `out/x.db`, picks the newest run of the week that measured anything | — (no run that week: **skipped**) |
| load | `ops.pipeline.step_load` (upsert of that run); row counts before/after logged | load error |
| diff | `ops.pipeline.step_diff` (SQL report == Python report) | the two disagree |
| checks | `mart.v_validation_issues` | any `error` row |
| export_s3 | `mart.v_latest`, `v_changes`, `v_runs` -> Parquet -> S3, today's `dt` | — (no AWS keys, or a backfill: **skipped**; the views are current state, not "as of" a past week) |
| athena_tables | `CREATE DATABASE/EXTERNAL TABLE IF NOT EXISTS` by DDL (no Glue crawler), `ADD PARTITION` | Athena error |
| athena_checks | `../dashboards/checks.sql` in Postgres and the same nine figures in Athena | any figure differs |

Retries 3 × 30 s; one Slack message after the last try (same `ops.notify` as the pipeline).
`params.fail_at=<task>` rehearses a failure; `params.run_id` pins a collector run.

`xmetrics_af_check` — read-only smoke check (guard, imports, row counts).

`media_lake_glue` — starts the AWS Glue Spark job over media-discovery's podcast lake
(`s3://…/media/podcasts/`, 4.7 M rows of Parquet), waits for it, and compares its figures with Postgres.
Code: media-discovery `discovery/glue.py`, mounted read-only at `/opt/media`. No schedule (the lake is a
one-off load of a public dump); trigger from the UI.

    start_glue -> wait_glue -> compare_postgres

| task | retries | why |
|---|---|---|
| start_glue | 0 | a retry would start, and pay for, a second Spark run |
| wait_glue | 2 | only reads the run's state; fails unless the run ends `SUCCEEDED` |
| compare_postgres | 1 | read only; fails if any Spark figure differs from Postgres |

## Measured on 2026-10-13 (진우's PC, copy database)

- Weekly run for 10-06..10-13 (`scheduled__2026-10-12`: started by the scheduler the moment the DAG was unpaused, because Airflow runs the latest finished interval; no later interval has come yet, the next is 10-19): run `20261008T023253Z`, 7 flagged, SQL == Python 19 of 19, validation error 0 / warn 19; row counts unchanged by the load.
- Backfill 2026-09-01..10-06: 5 runs — 3 loaded (09-06, 09-18, 09-23 collections), 2 skipped (week of 09-07: no collection; week of 09-28: the 10-04 run died before writing anything). Row counts unchanged by every load. Read that for what it is: the copy was restored from a backup that already held those runs, so the backfill re-loaded existing weeks — it shows the load is idempotent and that empty weeks are skipped, not that it filled a gap.
- Failure rehearsal `fail_at=load`: 4 tries ~30 s apart, one Slack message after the 4th, diff/checks never ran.
- S3 + Athena: 3 Parquet files (95 / 19 / 6 rows, 13.2 / 8.9 / 4.2 KB); the nine dashboard figures **9 of 9 equal** in Postgres and Athena; Athena scanned 2,714 bytes for them.
- `media_lake_glue` (13:52 KST): all three tasks succeeded in 2 min 25 s. Glue 5.1, 2 × G.1X, 127 s execution, 255 DPU-seconds ≈ USD 0.031; Spark == Postgres **5 of 5** (rows 4,731,573, duplicate Apple ids 1,656, duplicate URL keys 3,756, 168 per-year cells, top 10 languages). Screenshot `../docs/airflow-media-lake-glue.png`.

Evidence (the task logs in `logs/` are git-ignored; these are committed):

| claim | where |
|---|---|
| 10 runs: 5 backfill, 1 scheduled, 4 manual (one failed on purpose) | `../docs/airflow-postprocess-runs.png` |
| rehearsal: `load` failed after 4 tries, diff/checks `upstream_failed` | `../docs/airflow-retry-rehearsal.png` |
| every line above, from the logs: row counts before/after each load, the skipped weeks, the four `RuntimeError: rehearsal: fail_at=load` at 01:44:09, 01:44:40, 01:45:11, 01:45:42 UTC, the nine Athena = Postgres lines, the Glue run | `../docs/airflow-log-excerpts-2026-10-13.txt` (made by `python tools/airflow_log_excerpts.py`) |

The Airflow log viewer shows only two of the nine `OK` lines of `athena_checks`; the log file has all nine (see the excerpts).

## Start (Windows cmd)

    cd /d <repo>
    call ops\env.cmd & .venv\Scripts\activate & set XMETRICS_PG_DOCKER=postgres
    python -m ops.backup drill --into xmetrics_af --keep      (once: the copy)
    cd airflow
    notepad .env      XMETRICS_SLACK_WEBHOOK=…  AWS_ACCESS_KEY_ID=…  AWS_SECRET_ACCESS_KEY=…   (git-ignored)
    docker compose up -d --build

Backfill:

    docker compose exec airflow-scheduler airflow backfill create --dag-id xmetrics_postprocess --from-date 2026-09-01 --to-date 2026-10-06

Not claimed: Airflow in production, Airflow updating the production database, a Glue crawler, a data lake beyond these three snapshot tables.
