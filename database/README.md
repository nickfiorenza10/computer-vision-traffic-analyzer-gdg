# Database — Sebastian Ward

This is the data-layer handoff for the Python pipeline and Java API: an events
contract, PostgreSQL schema, CSV importer, and aggregation functions. It does not
require the detection/GPU packages. Run commands from the repository root.

## 1. Start PostgreSQL and apply the schema

Install Python 3.13 and Docker Desktop with Compose, then use PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r database/requirements.txt
docker compose -f database/compose.yaml up -d --wait
docker compose -f database/compose.yaml cp database/schema.sql postgres:/tmp/schema.sql
docker compose -f database/compose.yaml exec -T postgres psql -U traffic -d traffic -v ON_ERROR_STOP=1 -f /tmp/schema.sql
$env:DATABASE_URL = 'postgresql://traffic:traffic_dev@localhost:5432/traffic'
```

Reuse the project's `.venv` if it already exists. The Compose database stores data
in a named volume and binds only to localhost. `traffic_dev` is a local development
password; set `POSTGRES_PASSWORD` before the **first** start to change it, and match
the password in `DATABASE_URL`. Changing it later does not reset an existing volume's
password. Set `POSTGRES_PORT` before starting if port 5432 is busy.

For an existing PostgreSQL **16 or newer** database, instead run:

```powershell
$env:DATABASE_URL = 'postgresql://USER:PASSWORD@HOST:5432/DATABASE'
psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f database/schema.sql
```

Use a database you own. The script creates the `traffic` schema inside it. It can
be reapplied for this initial version; `CREATE TABLE IF NOT EXISTS` does **not**
migrate an older table definition. Future table changes need versioned migrations.
Compose uses PostgreSQL 17; production credentials and provisioning are separate.

## 2. Validate and import events

The sample's clip start is synthetic and has no recorded timezone. These examples
**assign UTC for the demo**; they do not infer the camera's actual timezone.

```powershell
python -m database.load_events data/sample_events.csv --naive-offset=+00:00 --validate-only
python -m database.load_events data/sample_events.csv --source-id sample-vehicles-2 --camera-id sample-camera --naive-offset=+00:00
```

Expected first import: `Imported: 787 events (sample-vehicles-2)`. Running the same
command again reports `Already imported: 787 events (sample-vehicles-2)` and adds
no rows. Validate-only uses Python's standard library and needs no database or
driver installation.

For new footage:

```powershell
python pipeline/track_count.py data/my_camera.mp4 --lanes pipeline/lanes_my_camera.json --start 2026-10-09T08:00:00-04:00
python -m database.load_events data/output/my_camera_events.csv --source-id camera1-20261009-0800 --camera-id camera1
```

When `--start` includes an offset, it is carried into the CSV, so no fallback is
needed. For an existing CSV without offsets, supply the recording's known offset,
for example `--naive-offset=-04:00`. Offsets already present in individual rows take
precedence. The loader rejects naive timestamps without a fallback and normalizes
all times to UTC. A fixed fallback applies to the whole clip; footage spanning a
daylight-saving transition must contain correct per-row offsets or be split first.

### Source identity and retries

- `source_id` is a globally unique, stable ID for one recording's counting output.
  Reuse it for retries, even when the file moves. Tracker IDs are unique only within
  that source; lane IDs are local to a camera's lane layout.
- `camera_id` identifies one camera and lane layout. Use a new ID if the layout
  changes. Queries combine all imported sources with the requested camera ID.
- A source stores the exact CSV's SHA-256, camera, fallback offset, row count,
  filename, and import time. Identical content and interpretation are a no-op;
  changed content or metadata under an existing source ID is rejected.
- The loader snapshots the file, validates it, and uses PostgreSQL `COPY` inside
  one transaction. Any malformed row or database failure rolls back both source
  and events. Concurrent retries with the same ID are arbitrated by its primary key.
- Do not assign a new ID to rerun output for footage already loaded. This would
  double-count vehicles. Corrected output needs a deliberate replacement procedure
  agreed with the database owner; automatic replacement is not provided.
- Overlapping clips can count the same physical vehicle twice. Source identity
  prevents repeat imports, not physical-vehicle matching across recordings.
- A header-only CSV is valid and imports a source with zero events. Keep the complete
  six-column header even when the pipeline detects no vehicles.

Validation streams records; a temporary disk snapshot holds the CSV and a set of
tracker IDs detects duplicates. Memory for that set grows with unique vehicles.
Only complete pipeline output files should be imported.

## CSV contract and database mapping

The exact, ordered header is:

```csv
timestamp,frame,track_id,class,lane,direction
```

- `timestamp` → `occurred_at`: ISO 8601 date and time with seconds, optionally 1–6
  fractional digits and `Z` or `±HH:MM`. Real-world clip start plus frame/FPS;
  stored as PostgreSQL `timestamptz`.
- `frame` → `frame`: zero-based, nonnegative signed 64-bit integer.
- `track_id` → `track_id`: nonnegative signed 64-bit tracker ID. One event per
  tracker per source, matching `counted_ids` in `pipeline/track_count.py`.
- `class` → `vehicle_class`: `car`, `truck`, `bus`, `motorcycle`, or `bicycle`.
- `lane` → `lane`: positive signed 32-bit integer from the source's lane file.
- `direction` → `direction`: nonblank text without surrounding whitespace.
  Labels are configurable in `lanes.json`; `inbound`/`outbound` and `in`/`out` work.

Every field is required. Unknown, extra, missing, or reordered columns are rejected
so a pipeline contract change is visible. UTF-8 CSVs with or without a BOM work.
The contract test reads the pipeline's constants without importing ML dependencies
and fails when its columns or vehicle classes change. Update this document, loader,
schema constraints, and tests together when changing that contract.

`traffic.sources` holds provenance. `traffic.events` holds the six typed event
fields plus the source foreign key. Its primary key is `(source_id, track_id)`.
Indexes support camera lookup and time-range scans within each source. Lane geometry
stays in the lane JSON files; the database stores crossing events, not trajectories.

## 3. Query counts and time bins

Run the checked-in examples after loading the sample with the UTC demo offset:

```powershell
docker compose -f database/compose.yaml cp database/queries.sql postgres:/tmp/queries.sql
docker compose -f database/compose.yaml exec -T postgres psql -U traffic -d traffic -v ON_ERROR_STOP=1 -f /tmp/queries.sql
```

With native `psql`, use `psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f database/queries.sql`.

The API can call these functions using bound JDBC parameters:

```sql
SELECT * FROM traffic.lane_totals(
    CAST(? AS text), CAST(? AS timestamptz), CAST(? AS timestamptz)
);
-- Columns: lane (integer), direction (text), vehicle_count (bigint)

SELECT * FROM traffic.time_bins(
    CAST(? AS text), CAST(? AS timestamptz), CAST(? AS timestamptz), CAST(? AS integer)
);
-- Columns: bin_start (timestamptz), lane, direction, vehicle_count
```

Bind `(cameraId, start, end)` or `(cameraId, start, end, binSeconds)`; use Java
`OffsetDateTime` for timestamps and `long` for counts. Inputs must include a camera
and finite timestamps with `start < end`. Time bins accept 1–86400 seconds; invalid
inputs raise SQLSTATE `22023`, which the API should map to a bad-request response.
The API should require explicit offsets and validate inputs before running queries.

Both queries use the half-open window **`[start, end)`**. An event at the end belongs
to the next request, preventing boundary double-counts. Bins align to the UTC Unix
epoch using PostgreSQL [`date_bin`](https://www.postgresql.org/docs/17/functions-datetime.html#FUNCTIONS-DATETIME-BIN).
A bin's label can precede `start` when a request cuts through a bin; its count still
includes only events inside the requested window. Set the API database session
timezone to UTC and serialize `bin_start` with an explicit offset.

Results group by lane and direction and include only groups with events. Unknown
cameras or empty ranges of data return no rows. Missing bins are **not** evidence
of a working camera with zero traffic: recording coverage is not yet stored. The
frontend should treat gaps as missing data until the team defines coverage. These
are vehicle crossing counts; congestion, speed, and occupancy cannot be determined
from this CSV alone. `queries.sql` also includes a class breakdown.

For the sample, lane totals sum to 787, and the 10-second bin totals also sum to
787. The integration test checks the full breakdown against the CSV independently.

### Backend handoff

Monisha can use the PostgreSQL JDBC driver and Spring datasource settings:

```properties
spring.datasource.url=${TRAFFIC_JDBC_URL}
spring.datasource.username=${TRAFFIC_DB_USER}
spring.datasource.password=${TRAFFIC_DB_PASSWORD}
spring.jpa.hibernate.ddl-auto=none
```

For Compose, `TRAFFIC_JDBC_URL` is `jdbc:postgresql://localhost:5432/traffic`.
Keep credentials in environment variables, apply `schema.sql` separately, and let
the database own the schema. The backend should use a separate read-only login in
shared deployments. Its administrator can grant that login `USAGE` on schema
`traffic` and `SELECT` on `traffic.sources` and `traffic.events`; the functions use
invoker permissions. The Compose `traffic` owner is for local setup/imports.

API endpoint names and JSON DTOs remain Monisha's responsibility. Agree on camera
IDs, direction labels, request limits, and missing-bin behavior before connecting
the dashboard. Michael's pipeline output does not need new columns.

## 4. Run checks

```powershell
python -m unittest discover -s database/tests -v
```

Without `TEST_DATABASE_URL`, the CSV tests run and PostgreSQL tests are reported
as skipped. To run every test, point it at an explicitly **disposable test database**
that your user owns, never production. Tests apply the schema/functions and remove
only their own source/event rows afterward:

```powershell
docker compose -f database/compose.yaml exec -T postgres createdb -U traffic traffic_test
$env:TEST_DATABASE_URL = 'postgresql://traffic:traffic_dev@localhost:5432/traffic_test'
python -m unittest discover -s database/tests -v
```

CI runs both suites on Python 3.13 and PostgreSQL 17. Coverage includes sample totals,
schema drift, malformed records, offsets, duplicate tracks, repeat imports, changed
sources, camera separation, transaction rollback, constraints, and bin boundaries.

Stop the local service with `docker compose -f database/compose.yaml down`.
The named volume remains for the next start.

## Team workflow

Work on `codex/sebastian-database`. Review the diff, commit and push that branch,
then open a PR against `main` and obtain at least one team review before merging.
Suggested PR title: `Add PostgreSQL storage, CSV importer, and traffic aggregations`.

Do not merge automatically. Share the importer command and the two function
signatures with Monisha and Michael. Open questions for the team are the real
camera timezone, recording/source naming, treatment of overlapping footage, and
the congestion metric. They do not prevent using this layer with the sample.
