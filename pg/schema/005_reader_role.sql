-- 005_reader_role.sql — a role that can read everything and write nothing.
--
-- The API (api/) connects as a member of this role, never as the owner. Whatever a request
-- does — and whatever a bug in the API lets a request do — the database refuses every
-- INSERT, UPDATE, DELETE, TRUNCATE and DDL from it. tests/test_api.py proves it by trying.
--
-- Why grant on raw and core, not only mart? The mart views call SQL functions
-- (mart.changes_since, mart.diff_row), and a function called from a view runs with the
-- *caller's* privileges, not the view owner's. So a reader needs SELECT on the tables those
-- functions touch. That is still read-only; it is just honest about what "reading the
-- dashboard" reads.
--
-- This file creates the group. Login users are created by `python -m pg.roles`, because a
-- password does not belong in a file that is committed.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xmetrics_reader') THEN
    CREATE ROLE xmetrics_reader NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA raw, core, mart TO xmetrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA raw, core, mart TO xmetrics_reader;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mart TO xmetrics_reader;

-- Objects the owner creates later (006_..., 007_...) get the same grants automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA raw, core, mart GRANT SELECT ON TABLES TO xmetrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA mart GRANT EXECUTE ON FUNCTIONS TO xmetrics_reader;

-- Nothing else: no INSERT/UPDATE/DELETE, no CREATE on any schema, no access to pg.schema_migrations.
