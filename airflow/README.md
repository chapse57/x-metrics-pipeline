# airflow/ — local Airflow on a copy of the database

For post-processing orchestration only (load -> core/mart -> diff -> checks -> Slack), on the
copy database `xmetrics_af`. Now (2026-10-13): the stack and one read-only check DAG. The
post-processing DAG comes after the 2026-10-14 unattended-run check. Collection stays with the Windows scheduled task: it needs the
logged-in browser on the desktop, which a Linux container cannot drive.

| | |
|---|---|
| Airflow | 3.3.2, LocalExecutor (official compose cut down: no Celery, Redis, Flower, triggerer) |
| Airflow's own records | its own Postgres (`airflow-db`, no port published) |
| Data it touches | `xmetrics_af` only — `dags/_xm.py` refuses any other database name |
| UI | http://localhost:8080 (airflow / airflow, laptop only) |

Start (Windows cmd, from the repo folder):

    python -m ops.backup drill --into xmetrics_af --keep     (once; needs ops\env.cmd, venv, XMETRICS_PG_DOCKER=postgres)
    cd airflow
    docker compose up -d --build

Then in the UI: Dags -> `xmetrics_af_check` -> Trigger. Three read-only tasks: the guard, the
imports, row counts.

Not claimed: Airflow in production, Airflow updating the production database.
