# airflow/ — local Airflow on a copy of the database, with S3 + Athena

Post-processing orchestration only, on the copy database `xmetrics_af`. Collection stays with the
Windows scheduled task: it needs the logged-in browser on the desktop, which a Linux container
cannot drive. Nothing here writes to the production database `xmetrics`.

| | |
|---|---|
| Airflow | 3.3.2, LocalExecutor (official compose cut down: no Celery, Redis, Flower, triggerer) |
| Airflow's own records | its own Postgres (`airflow-db`, no port published) |
| Data it touches | `xmetrics_af` only — `dags/_xm.py` refuses any other database name |
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

## Measured on 2026-10-13 (진우's PC, copy database)

- Weekly run for 10-06..10-13: run `20261008T023253Z`, 7 flagged, SQL == Python 19 of 19, validation error 0 / warn 19; row counts unchanged by the load.
- Backfill 2026-09-01..10-06: 5 runs — 3 loaded (09-06, 09-18, 09-23 collections), 2 skipped (week of 09-07: no collection; week of 09-28: the 10-04 run died before writing anything). Row counts unchanged by every load.
- Failure rehearsal `fail_at=load`: 4 tries ~30 s apart, one Slack message after the 4th, diff/checks never ran.
- S3 + Athena: 3 Parquet files (95 / 19 / 6 rows, 13.2 / 8.9 / 4.2 KB); the nine dashboard figures **9 of 9 equal** in Postgres and Athena; Athena scanned 2,714 bytes for them.

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
