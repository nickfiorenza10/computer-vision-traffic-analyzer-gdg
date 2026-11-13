# Traffic API

A read-only JSON API over the PostgreSQL database in [database/](../database/README.md),
written in Rust and deployed as one [Vercel Rust function](https://vercel.com/docs/functions/runtimes/rust)
(public beta). The React dashboard calls it from the same site, at `/api/...`.

```
Browser → /api/* → api/traffic.rs (Vercel function) → src/lib.rs router → PostgreSQL
```

| File | What it is |
|------|------------|
| `api/traffic.rs` | The Vercel function. `vercel.json` rewrites every `/api/*` request to it. |
| `src/lib.rs` | The Axum router, handlers and database pool. |
| `src/params.rs` | Query-parameter parsing and limits. |
| `src/error.rs` | Error responses (`{"error": "..."}`). |
| `src/fl511.rs` | The public Florida (FL511) camera list. |
| `src/map.rs` | The live traffic map: camera GeoJSON and the live-analysis proxy to the worker. |
| `examples/dev_server.rs` | The same router on a plain port, for local development. Not deployed. |
| `tests/api.rs` | Integration test against a real PostgreSQL database. |
| `tests/fl511.rs` | The FL511 endpoint end to end, against a fake FL511 server (no network, no database). |
| `tests/map.rs` | The map endpoint against PostgreSQL, and the live proxy against a fake worker. |

Counting rules stay in the database: the handlers call Sebastian's `traffic.lane_totals`
and `traffic.time_bins` functions, and the class breakdown query from
`database/queries.sql`.

## Endpoints

All responses are JSON. Times are RFC 3339 in UTC, ending in `Z`. Time windows are
half-open: `start <= t < end`.

| Method and path | Query parameters | Response |
|-----------------|------------------|----------|
| `GET /api/health` | none | `{"status":"ok","database":"ok"}`, or HTTP 503 with `"database":"error"` |
| `GET /api/cameras` | none | `{"cameras":[{"camera_id","sources","events","first_event","last_event"}]}` |
| `GET /api/lane_totals` | `camera_id`, `start`, `end` | `{"camera_id","start","end","totals":[{"lane","direction","vehicle_count"}]}` |
| `GET /api/time_bins` | `camera_id`, `start`, `end`, `bin_seconds` | `{"camera_id","start","end","bin_seconds","bins":[{"bin_start","lane","direction","vehicle_count"}]}` |
| `GET /api/classes` | `camera_id`, `start`, `end` | `{"camera_id","start","end","classes":[{"lane","direction","vehicle_class","vehicle_count"}]}` |

Example:

```
GET /api/lane_totals?camera_id=sample-camera&start=2026-10-09T08:00:00Z&end=2026-10-09T08:01:00Z

{"camera_id":"sample-camera","start":"2026-10-09T08:00:00Z","end":"2026-10-09T08:01:00Z",
 "totals":[{"lane":1,"direction":"inbound","vehicle_count":42}, ...]}
```

## Live traffic map

The main page's map uses these. Cameras and sweep results come from
`database/map_schema.sql` (`traffic.cameras`, `traffic.camera_sweeps`), which the worker
fills: a daily catalog refresh, and every 30 minutes 8 stills per camera in the sweep
districts (Sacramento, district 3, to start).

| Method and path | Response |
|-----------------|----------|
| `GET /api/map/cameras` | GeoJSON `FeatureCollection`, one Point per in-service camera, with properties `camera_id`, `name`, `county`, `route`, `direction`, `has_stream`, `image_url`, `swept`, `level` (`heavy`, `moderate`, `light`, `learning`, `night`, `unknown` or `null`), `vehicles_mean`, `swept_at`. Cached 60 s. |
| `POST /api/live` with `{"camera_id":"caltrans-D3-41"}` | 202 `{"session_id","state"}` · 409 busy (`retry_after_s`) · 503 `{"error":"live analysis is offline"}` · 401 if an access code is required and missing |
| `GET /api/live/{session_id}` | `{"state","camera_id","elapsed_s","level","flow_per_min","density","speed","detector","frame_seq","error"}` |
| `GET /api/live/{session_id}/frame.jpg` | The newest annotated frame (`image/jpeg`, `no-store`); 404 before the first one |
| `DELETE /api/live/{session_id}` | Stops the session |

Live analysis runs on the worker (`worker/`) on Michael's PC. The API reaches it through a
tunnel at `WORKER_URL` and sends `WORKER_TOKEN` itself, so the token never reaches
browsers. Whenever the worker is unreachable, live calls answer 503 and the map keeps
working from the stored sweeps. Busy, offline and access-code answers pass through
unchanged; any other worker failure is reported as offline.

## Florida cameras (public)

`GET /api/fl511/cameras` lists Tampa-area FL511 cameras. It runs the same query and
the same highway filter as `ingestion/fl511.py`. FDOT first authorized FL511 for academic
research through 2026-12-01. On **2026-10-29 FDOT confirmed in writing that the project may
use FL511 with no end date, public display included** (Michael has the email), and Michael
made Florida public the same day.

- **It needs no token.**
- **It has no end date.** `AUTHORIZED_UNTIL` in `src/fl511.rs` is `None`. If an
  authorization ever sets an end date again, put it there: after it, every request gets
  **403**. Change it only with a written authorization.
- **Vercel's edge caches the list for 5 minutes**
  (`Cache-Control: public, s-maxage=300, stale-while-revalidate=600`), so public visitors
  don't multiply the requests FL511 sees.

| Query parameter | Default | Meaning |
|-----------------|---------|---------|
| `bbox` | Tampa: `-82.60,27.80,-82.30,28.10` | `west,south,east,north` in degrees |
| `include_highways` | off | `true` to include interstate, expressway, ramp and mile-marker cameras |

Response:

```
{"source":"FL511","authorized_through":null,"bbox":[-82.6,27.8,-82.3,28.1],
 "cameras":[{"camera_id":"1043","description":"Dale Mabry at Hillsborough Ave",
             "road":"for City of Tampa cameras","direction":"N",
             "latitude":27.99,"longitude":-82.5,"image_url":"https://..."}]}
```

The frontend should fetch a camera's `image_url` only when someone has that camera open,
not for every camera on the map. Snapshot analysis isn't served by the API; it will run on the worker.

**Rules the frontend should rely on:**
- **Missing bins mean "no data", not zero traffic.** Lanes, directions or bins with no
  events are left out, and nothing is filled with zeros. An unknown camera or an empty
  window returns 200 with empty arrays.
- `start` and `end` must include an offset or `Z` (`2026-10-09T08:00:00Z`,
  `2026-10-09T04:00:00-04:00`). Times without one are rejected rather than guessed.
- Limits: the window is at most 31 days; `bin_seconds` must be 1–86400; and a
  `time_bins` request can cover at most 5,000 bins.
- Bad input returns **400** `{"error":"<plain message>"}`. Server problems return
  **500** `{"error":"internal server error"}`; details go to the function logs only.
- Data responses are cached for 30 seconds
  (`Cache-Control: public, s-maxage=30, stale-while-revalidate=60`). Errors and
  `/api/health` are never cached.

## Configuration

| Variable | Used by | Value |
|----------|---------|-------|
| `DATABASE_URL_READONLY` | the function and the dev server | A PostgreSQL URL for a **read-only** login (see "Backend handoff" in `database/README.md`). For hosted Postgres, use the pooled connection string, with `sslmode=require`. |
| `FL511_LAYER_URL` | tests | Optional override of the FL511 layer URL. Leave unset in production. |
| `WORKER_URL` | the function and the dev server | Optional. The worker's public tunnel URL, e.g. `https://<name>.ts.net`. Without it (or `WORKER_TOKEN`), live analysis is offline. |
| `WORKER_TOKEN` | the function and the dev server | Optional. The worker token (the same value the worker runs with). Secret. |
| `LIVE_ACCESS_CODE` | the function and the dev server | Optional. When set, `POST /api/live` needs `Authorization: Bearer <code>`; when unset, anyone can start a session (one at a time). |
| `PORT` | dev server only | Default `3001`. |

Never commit real connection strings. `.env.example` lists the names.

## Develop and test

Rust builds run in Docker (`rust:1-bookworm`), the same Linux environment Vercel
builds on, so nothing is needed on Windows except Docker Desktop. On Linux or macOS
with Rust installed, run the `cargo` commands directly.

Start the database and load the sample (once), following `database/README.md`:

```powershell
docker compose -f database/compose.yaml up -d --wait
```

Run the tests. The integration test is skipped unless `TEST_DATABASE_URL` is set,
like `database/tests`. It applies `database/schema.sql`, loads
`data/sample_events.csv` under its own camera ID, checks every endpoint against
counts computed from the CSV, and removes its own rows:

```powershell
docker run --rm --network database_default -v "${PWD}:/src" -w /src `
  -v traffic-cargo-registry:/usr/local/cargo/registry -v traffic-cargo-target:/target `
  -e CARGO_TARGET_DIR=/target `
  -e TEST_DATABASE_URL=postgresql://traffic:traffic_dev@postgres:5432/traffic_test `
  rust:1-bookworm cargo test
```

Run the dev server against the local database (with the sample imported under
`sample-camera`), then open http://localhost:3001/api/health:

```powershell
docker run --rm --network database_default -p 127.0.0.1:3001:3001 -v "${PWD}:/src" -w /src `
  -v traffic-cargo-registry:/usr/local/cargo/registry -v traffic-cargo-target:/target `
  -e CARGO_TARGET_DIR=/target `
  -e DATABASE_URL_READONLY=postgresql://traffic:traffic_dev@postgres:5432/traffic `
  rust:1-bookworm cargo run --example dev_server
```

The two named volumes cache downloaded crates and build output, so later runs only
recompile what changed. In Git Bash, prefix the command with `MSYS_NO_PATHCONV=1` so
paths like `/src` aren't rewritten.

Before committing, run `cargo fmt` and `cargo clippy --all-targets`.
