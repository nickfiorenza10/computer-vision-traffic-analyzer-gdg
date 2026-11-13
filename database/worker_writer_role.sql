-- Write login for the worker (worker/), which imports events.csv with load_events.py.
-- It gets exactly what the importer needs: read and insert on traffic.sources (to detect
-- a re-import), and insert on traffic.events (COPY). No UPDATE, DELETE, TRUNCATE or
-- schema changes, so a leaked worker secret can add rows but can't alter or remove data.
--
-- Run it as the database owner after schema.sql, with the password for the new login in
-- the WRITER_PASSWORD environment variable (see worker/README.md):
--
--   docker run --rm -e WRITER_PASSWORD -v "${PWD}:/work" -w /work postgres:17 \
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/worker_writer_role.sql
--
-- Safe to re-run: it re-applies the grants, and sets a new password only when
-- WRITER_PASSWORD is set.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'traffic_writer') THEN
        CREATE ROLE traffic_writer LOGIN CONNECTION LIMIT 5;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA traffic TO traffic_writer;
GRANT SELECT, INSERT ON traffic.sources TO traffic_writer;
GRANT INSERT ON traffic.events TO traffic_writer;

ALTER ROLE traffic_writer SET statement_timeout = '120s';

-- Password from WRITER_PASSWORD, so it never appears in this file or on a command line.
\getenv writer_password WRITER_PASSWORD
\if :{?writer_password}
SELECT format('ALTER ROLE traffic_writer PASSWORD %L', :'writer_password') \gexec
\echo 'traffic_writer is ready, with the password from WRITER_PASSWORD.'
\else
\echo 'traffic_writer is ready but has NO password: set WRITER_PASSWORD and run this again.'
\endif
