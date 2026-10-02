"""Backup and restore, for real: pg_dump the loaded database, pg_restore it into a new one,
compare every table's row count and checksum. Then change one value in the copy and check the
comparison notices. Needs XMETRICS_PG_DSN and pg_dump / pg_restore on PATH (CI installs them).
"""
from __future__ import annotations

import shutil

import pytest

psycopg = pytest.importorskip("psycopg")
from tests.test_api import DSN, SQLITE_FILES, database  # noqa: E402,F401

pytestmark = [pytest.mark.skipif(not DSN, reason="XMETRICS_PG_DSN not set"),
              pytest.mark.skipif(not SQLITE_FILES, reason="no out/*.db to load"),
              pytest.mark.skipif(not shutil.which("pg_dump"), reason="pg_dump not on PATH")]

COPY = "xmetrics_restore_test"


@pytest.fixture
def restored(database, tmp_path):
    from ops.backup import dump, restore
    from pg.roles import create_key
    with psycopg.connect(DSN) as conn:
        create_key(conn, "backup-test")                     # so auth.* has rows to carry over too
    d = dump(DSN, tmp_path / "x.dump")
    restore(DSN, tmp_path / "x.dump", COPY)
    yield d
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {COPY}")


def test_restored_copy_is_identical(restored):
    from ops.backup import fingerprint, other_db, verify
    assert restored["bytes"] > 0
    assert verify(DSN, COPY) == []
    fp = fingerprint(other_db(DSN, COPY))
    assert {"raw.measurements", "raw.run_targets", "ops.pipeline_runs", "auth.api_key", "auth.request_log"} <= set(fp)
    assert fp["raw.measurements"][0] > 0 and fp["auth.api_key"][0] > 0


def test_one_changed_value_is_caught(restored):
    from ops.backup import other_db, verify
    with psycopg.connect(other_db(DSN, COPY)) as c:
        c.execute("UPDATE raw.measurements SET followers = followers + 1 "
                  "WHERE ctid = (SELECT ctid FROM raw.measurements LIMIT 1)")
    assert verify(DSN, COPY) == ["raw.measurements: same row count ({}), different contents".format(
        psycopg.connect(DSN).execute("SELECT count(*) FROM raw.measurements").fetchone()[0])]


def test_restored_copy_keeps_the_read_only_role(restored):
    """Grants travel with the dump: in the copy, the reader still reads and still cannot write."""
    from ops.backup import other_db
    from tests.test_api import reader_dsn
    with psycopg.connect(other_db(reader_dsn(DSN), COPY), autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM mart.v_latest").fetchone()[0] > 0
        c.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM raw.measurements")
