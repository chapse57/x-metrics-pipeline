# Airflow 3.3.2 (stable, released 2026-09-17) plus the one library the x-metrics code needs.
# psycopg 3 is what pg/ and ops/ import; the stock image ships only psycopg2 for Airflow itself.
# Pinning apache-airflow in the same pip call stops pip from up- or downgrading Airflow.
FROM apache/airflow:3.3.2
RUN pip install --no-cache-dir "apache-airflow==3.3.2" "psycopg[binary]>=3.1,<4"
