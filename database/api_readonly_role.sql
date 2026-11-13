-- Read-only login for the Rust API (api/README.md), following "Backend handoff" in
-- database/README.md: USAGE on schema traffic, SELECT on the two tables, and the two
-- count functions. Run it as the database owner after schema.sql, with the password for
-- the new login in the API_PASSWORD environment variable (see DEPLOY.md, step 2):
--
--   docker run --rm -e API_PASSWORD -v "${PWD}:/work" -w /work postgres:17 \
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/api_readonly_role.sql
--
-- Safe to re-run: it re-applies the grants, and sets a new password only when
-- API_PASSWORD is set.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'traffic_api') THEN
        CREATE ROLE traffic_api LOGIN CONNECTION LIMIT 20;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA traffic TO traffic_api;
GRANT SELECT ON traffic.sources, traffic.events TO traffic_api;
GRANT EXECUTE ON FUNCTION
    traffic.lane_totals(text, timestamptz, timestamptz),
    traffic.time_bins(text, timestamptz, timestamptz, integer)
TO traffic_api;

-- Even if a grant above is ever widened by mistake, this login can't write.
ALTER ROLE traffic_api SET default_transaction_read_only = on;
ALTER ROLE traffic_api SET statement_timeout = '5s';

-- Password: taken from the API_PASSWORD environment variable when it's set, so it never
-- appears in this file or on a command line. Neon accepts it this way (its dashboard
-- can't reset a role that has no password yet, and it rejects psql's \password).
\getenv api_password API_PASSWORD
\if :{?api_password}
SELECT format('ALTER ROLE traffic_api PASSWORD %L', :'api_password') \gexec
\echo 'traffic_api is ready, with the password from API_PASSWORD.'
\else
\echo 'traffic_api is ready but has NO password: set API_PASSWORD and run this again.'
\endif
