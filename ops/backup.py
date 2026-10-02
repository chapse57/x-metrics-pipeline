"""Back up the database, restore it somewhere else, and prove the copy is the same.

A backup nobody has restored is a hope. This restores into a fresh database and compares
every table in every schema we own: row count and a checksum over all rows (md5 of each row's
text, sorted, then md5 of the lot). One differing value anywhere changes the checksum.

    python -m ops.backup dump    --out backups\\x-2026-10-02.dump          # pg_dump -Fc
    python -m ops.backup restore --file backups\\x-2026-10-02.dump --into xmetrics_restore
    python -m ops.backup verify  --into xmetrics_restore                   # exit 1 on any difference
    python -m ops.backup drill                                             # all three, then drop the copy

Owner DSN from XMETRICS_PG_DSN. pg_dump / pg_restore come from PATH. With only Docker (the
usual case on Windows), set XMETRICS_PG_DOCKER=postgres and they run inside that compose
service instead, with the dump streamed through stdin/stdout:

    set XMETRICS_PG_DOCKER=postgres
    python -m ops.backup drill
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import psycopg.conninfo as ci
from psycopg import sql

SCHEMAS = ("raw", "core", "mart", "ops", "auth", "pg")


def other_db(dsn: str, dbname: str) -> str:
    parts = ci.conninfo_to_dict(dsn)
    parts["dbname"] = dbname
    return ci.make_conninfo(**parts)


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"{name} not on PATH (install the PostgreSQL client, or run it in the container)")
    return path


def _in_docker(dsn: str, tool: str, *args: str) -> list[str]:
    """The same tool inside the compose service, as the DSN's user, on the container's socket."""
    p = ci.conninfo_to_dict(dsn)
    return ["docker", "compose", "exec", "-T", os.environ["XMETRICS_PG_DOCKER"], tool, "-U", p["user"], *args]


def dump(dsn: str, out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    if os.environ.get("XMETRICS_PG_DOCKER"):
        with open(out, "wb") as f:
            subprocess.run(_in_docker(dsn, "pg_dump", "-Fc", "-d", ci.conninfo_to_dict(dsn)["dbname"]), stdout=f, check=True)
    else:
        subprocess.run([_tool("pg_dump"), "-Fc", "-f", str(out), "-d", dsn], check=True)
    return {"file": str(out), "bytes": out.stat().st_size, "seconds": round(time.perf_counter() - t, 2)}


def restore(dsn: str, file: Path, into: str) -> dict:
    """Into a database that is created fresh: an existing one of that name is dropped first."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(into)))
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(into)))
    t = time.perf_counter()
    if os.environ.get("XMETRICS_PG_DOCKER"):
        with open(file, "rb") as f:
            subprocess.run(_in_docker(dsn, "pg_restore", "--exit-on-error", "-d", into), stdin=f, check=True)
    else:
        subprocess.run([_tool("pg_restore"), "--exit-on-error", "-d", other_db(dsn, into), str(file)], check=True)
    return {"into": into, "seconds": round(time.perf_counter() - t, 2)}


def fingerprint(dsn: str) -> dict[str, tuple[int, str]]:
    """{schema.table: (rows, checksum)} for every base table in SCHEMAS."""
    out = {}
    with psycopg.connect(dsn) as c:
        tables = c.execute("SELECT table_schema, table_name FROM information_schema.tables "
                           "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s) ORDER BY 1, 2",
                           (list(SCHEMAS),)).fetchall()
        for schema, table in tables:
            t = sql.Identifier(schema, table)
            n, h = c.execute(sql.SQL("SELECT count(*), coalesce(md5(string_agg(h, '' ORDER BY h)), md5('')) "
                                     "FROM (SELECT md5(x::text) AS h FROM {} x) s").format(t)).fetchone()
            out[f"{schema}.{table}"] = (n, h)
    return out


def verify(dsn: str, into: str) -> list[str]:
    """Every difference between the source and the restored copy, as sentences. Empty = identical."""
    a, b = fingerprint(dsn), fingerprint(other_db(dsn, into))
    problems = [f"{t}: missing in the copy" for t in a if t not in b]
    problems += [f"{t}: only in the copy" for t in b if t not in a]
    for t in sorted(set(a) & set(b)):
        if a[t][0] != b[t][0]:
            problems.append(f"{t}: {a[t][0]} rows, copy has {b[t][0]}")
        elif a[t][1] != b[t][1]:
            problems.append(f"{t}: same row count ({a[t][0]}), different contents")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn", default=os.environ.get("XMETRICS_PG_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump"); d.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("restore"); r.add_argument("--file", type=Path, required=True); r.add_argument("--into", required=True)
    v = sub.add_parser("verify"); v.add_argument("--into", required=True)
    dr = sub.add_parser("drill"); dr.add_argument("--out", type=Path, default=Path("out/backups/drill.dump"))
    dr.add_argument("--into", default="xmetrics_restore_drill"); dr.add_argument("--keep", action="store_true")
    a = p.parse_args(argv)
    if not a.dsn:
        sys.exit("set XMETRICS_PG_DSN or pass --dsn")
    if a.cmd == "dump":
        print(dump(a.dsn, a.out)); return 0
    if a.cmd == "restore":
        print(restore(a.dsn, a.file, a.into)); return 0
    if a.cmd == "verify":
        problems = verify(a.dsn, a.into)
        print("\n".join(problems) or f"identical: {len(fingerprint(a.dsn))} tables, rows and checksums match")
        return 1 if problems else 0
    d1 = dump(a.dsn, a.out)
    r1 = restore(a.dsn, a.out, a.into)
    problems = verify(a.dsn, a.into)
    fp = fingerprint(a.dsn)
    print({"dump": d1, "restore": r1, "tables": len(fp), "rows": sum(n for n, _ in fp.values()),
           "identical": not problems, "problems": problems})
    if not a.keep:
        with psycopg.connect(a.dsn, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(a.into)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
