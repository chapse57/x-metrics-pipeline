"""mart views -> Parquet on S3 -> Athena external tables, and the nine dashboard figures read back from Athena.

Not a DAG (leading underscore; listed in .airflowignore). Used by xmetrics_postprocess.

Layout:  s3://<bucket>/xmetrics/<table>/dt=YYYY-MM-DD/part-0.parquet
         one snapshot of the view per day (a re-run the same day overwrites it), so Athena sees
         the mart as it was on that day. Tables: latest (mart.v_latest), changes (mart.v_changes),
         runs (mart.v_runs).
Athena:  database xmetrics_lake, tables created by DDL here (no Glue crawler), partition added
         per snapshot. Query results go to s3://<bucket>/athena-results/.

Column types come from Postgres type oids, not from the values, so a snapshot with no rows or
a NULL column still writes the same schema as every other day.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal

BUCKET = os.environ.get("XMETRICS_LAKE_BUCKET", "ether-xmetrics-lake")
PREFIX = "xmetrics"
DATABASE = "xmetrics_lake"
WORKGROUP = "primary"
RESULTS = f"s3://{BUCKET}/athena-results/"
VIEWS = {"latest": "mart.v_latest", "changes": "mart.v_changes", "runs": "mart.v_runs"}


def configured() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))


# Postgres oid -> (arrow type, athena type). Anything not listed is written as text.
def _types():
    import pyarrow as pa
    s, d, i8, i4, b = pa.string(), pa.float64(), pa.int64(), pa.int32(), pa.bool_()
    ts = pa.timestamp("us")
    return {
        16: (b, "boolean"), 20: (i8, "bigint"), 21: (i4, "int"), 23: (i4, "int"),
        700: (d, "double"), 701: (d, "double"), 1700: (d, "double"),     # numeric -> double, stated in the README
        25: (s, "string"), 1043: (s, "string"), 1042: (s, "string"),
        1114: (ts, "timestamp"), 1184: (ts, "timestamp"),                 # timestamptz -> UTC, no zone
        1082: (pa.date32(), "date"),
        1009: (pa.list_(s), "array<string>"), 1015: (pa.list_(s), "array<string>"),
        1007: (pa.list_(i4), "array<int>"),
    }


def _value(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(v, (dict,)):
        return json.dumps(v, ensure_ascii=False)
    return v


def snapshot(conn, view: str):
    """Rows of one view as an Arrow table, plus [(column, athena type)] for the DDL."""
    import pyarrow as pa
    types = _types()
    cur = conn.execute(f"SELECT * FROM {view}")
    cols = [(c.name, types.get(c.type_code, (pa.string(), "string"))) for c in cur.description]
    rows = cur.fetchall()
    data = {name: [_value(r[i]) for r in rows] for i, (name, _) in enumerate(cols)}
    for name, (atype, athena) in cols:
        if athena == "string":
            data[name] = [None if v is None else (v if isinstance(v, str) else str(v)) for v in data[name]]
    table = pa.table({name: pa.array(data[name], type=atype) for name, (atype, _) in cols})
    return table, [(name, athena) for name, (_, athena) in cols]


def put_parquet(s3, table, name: str, dt: str) -> dict:
    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    key = f"{PREFIX}/{name}/dt={dt}/part-0.parquet"
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())
    return {"key": key, "rows": table.num_rows, "bytes": buf.tell()}


def ddl(name: str, columns: list[tuple[str, str]]) -> str:
    cols = ",\n  ".join(f"`{c}` {t}" for c, t in columns)
    return (f"CREATE EXTERNAL TABLE IF NOT EXISTS {DATABASE}.{name} (\n  {cols}\n)\n"
            f"PARTITIONED BY (dt string)\nSTORED AS PARQUET\n"
            f"LOCATION 's3://{BUCKET}/{PREFIX}/{name}/'")


def add_partition(name: str, dt: str) -> str:
    return (f"ALTER TABLE {DATABASE}.{name} ADD IF NOT EXISTS PARTITION (dt = '{dt}') "
            f"LOCATION 's3://{BUCKET}/{PREFIX}/{name}/dt={dt}/'")


def athena(client, sql: str, database: str | None = DATABASE, timeout: int = 120) -> dict:
    """Run one statement, wait, return {'rows': [...], 'scanned_bytes': n, 'ms': n}."""
    kw = {"QueryString": sql, "WorkGroup": WORKGROUP, "ResultConfiguration": {"OutputLocation": RESULTS}}
    if database:
        kw["QueryExecutionContext"] = {"Database": database}
    qid = client.start_query_execution(**kw)["QueryExecutionId"]
    t0 = time.time()
    while True:
        q = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() - t0 > timeout:
            client.stop_query_execution(QueryExecutionId=qid)
            raise TimeoutError(f"Athena query {qid} still {state} after {timeout}s")
        time.sleep(1)
    if state != "SUCCEEDED":
        raise RuntimeError(f"Athena {state}: {q['Status'].get('StateChangeReason', '')}\n{sql[:300]}")
    rows = []
    if q.get("StatementType") == "DML":
        res = client.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
        rows = [[c.get("VarCharValue") for c in r["Data"]] for r in res]
    st = q.get("Statistics", {})
    return {"id": qid, "rows": rows, "scanned_bytes": st.get("DataScannedInBytes", 0),
            "ms": st.get("EngineExecutionTimeInMillis", 0)}


def checks_sql(dt: str) -> str:
    """dashboards/checks.sql, the same nine figures, over the dt snapshot."""
    p = f"dt = '{dt}'"
    return f"""SELECT
  (SELECT count(*) FROM latest WHERE {p} AND status = 'measured') AS accounts_measured,
  (SELECT round(avg(engagement_rate), 4) FROM latest WHERE {p} AND status = 'measured') AS avg_engagement_pct,
  (SELECT max(followers) FROM latest WHERE {p} AND status = 'measured') AS max_followers,
  (SELECT count(*) FROM changes WHERE {p}) AS changes_rows,
  (SELECT count(*) FROM changes WHERE {p} AND cardinality(flags) > 0) AS changes_flagged,
  (SELECT count(*) FROM changes WHERE {p} AND kind = 'dropped') AS changes_dropped,
  (SELECT run_id FROM runs WHERE {p} AND recency = 1) AS latest_run,
  (SELECT taken_at FROM runs WHERE {p} AND recency = 1) AS data_taken_at_utc,
  (SELECT CAST(floor((to_unixtime(current_timestamp) - to_unixtime(taken_at)) / 86400) AS integer)
     FROM runs WHERE {p} AND recency = 1) AS age_days"""


NAMES = ["accounts_measured", "avg_engagement_pct", "max_followers", "changes_rows", "changes_flagged",
         "changes_dropped", "latest_run", "data_taken_at_utc", "age_days"]


def normalise(name: str, v) -> str | None:
    """Both sides as the same text: counts as integers, the rate to 4 places, the time to the second (UTC)."""
    if v is None:
        return None
    if name == "avg_engagement_pct":
        return f"{float(v):.4f}"
    if name == "data_taken_at_utc":
        if isinstance(v, datetime):
            v = (v.astimezone(timezone.utc) if v.tzinfo else v).strftime("%Y-%m-%d %H:%M:%S")
        return str(v)[:19].replace("T", " ")
    if name == "latest_run":
        return str(v)
    return str(int(float(v)))
