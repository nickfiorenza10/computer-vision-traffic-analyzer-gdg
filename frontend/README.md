# Dashboard

The React frontend for the traffic analyzer: counts by lane, direction, class and time
from the [API](../api/README.md), plus a replay panel that draws the pipeline's boxes,
track IDs, lanes and counts over a video you pick from your own disk.

```
frontend/src
  App.tsx            data loading, camera/range controls, layout
  api.ts             typed client for /api/* (same origin; Vite proxy in dev)
  types.ts           wire types, mirroring api/README.md
  lib/bins.ts        automatic bin-size choice
  lib/results.ts     results-JSON parsing, frame lookup, per-frame counts
  lib/aggregate.ts   KPIs, lane bars, bin rows, class tables
  components/        one file per view, plus loading/empty/error states
  mocks/             fixtures captured from the API, for offline UI work
```

## Run it

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173, proxies /api to the local API on :3001
```

Start the Rust API and the database first (see [api/README.md](../api/README.md) and
[database/README.md](../database/README.md)). Without them the page still loads and each
data view shows its error state.

Working on the layout without a database:

```bash
VITE_USE_MOCKS=1 npm run dev
```

The fixtures in `src/mocks/data/` were captured from the real endpoints against the
sample camera, so the shapes match the contract exactly.

```bash
npm test             # Vitest: bin sizing, results parsing, frame lookup, aggregation
npm run build        # type-check and build to dist/
```

## The two views

The app has a **Map** and a **History** tab; the tab lives in the URL hash (`#history`), so
either view can be linked to.

* **Map** (the default) is the product: California's Caltrans cameras on MapLibre GL JS with
  OpenFreeMap's vector tiles (no key, OSM attribution kept). Clicking a camera flies to it,
  shrinks the map to 55% and opens a panel that shows the last sweep and, if the camera has a
  stream, starts a live analysis by itself; on a phone the panel is a bottom sheet.

  The toggle switches between two different drawings, on purpose:

  | mode | what is drawn | why |
  |---|---|---|
  | Traffic level | one large, blurred, low-opacity circle per camera **that has a traffic reading**, in that level's colour | the level belongs to a road, not to how many cameras are nearby |
  | Camera coverage | a density heatmap, every camera weighted the same, plus the points | here counting cameras *is* the point |

  Both modes also draw the camera points in their own colours (grey for no sweep, violet
  `learning`, slate `night`) from zoom 7. Cameras with `night`, `learning` or no sweep are left
  out of the traffic wash entirely — weighting them is what once made a hundred night cameras
  glow red along US-50 while the legend said zero heavy. When nothing on the map has a reading,
  a line says so: "No traffic readings yet: cameras are learning or it's dark."
* **History** is the charts dashboard the app started as: volume by lane and bin, lane
  comparison, class breakdown and the replay player.

### The camera panel

| what the API says | what the panel does |
|---|---|
| `202` on `POST /api/live` | polls `GET /api/live/{id}` every second, swaps `frame.jpg` twice a second, and shows the verdict with flow, density and speed once the first 10 s window closes |
| `409` | says another analysis is running, how long it needs, and shows the sweep result — no polling |
| `503` + `error_code: "stream_offline"` | says "Video unavailable from Caltrans right now", shows the sweep result, and offers **Try again** |
| `503` without that code | says live analysis is offline and shows the sweep result (no retry: the worker, not the camera, is the problem) |
| camera without `has_stream` | the sweep result only, with a note |
| `done` / `failed` | keeps the last frame and says why it stopped; a failure offers **Try again** |

Only the worker's own sentence is ever shown. A dead stream is a 503 with an
`error_code`, never the decoder's text or an exception name.

Closing the panel (or pressing Escape) sends `DELETE /api/live/{id}`, which is what tells the
worker to stop.

## Views

| View | What it shows |
|------|---------------|
| Header controls | Camera picker from `/api/cameras`, UTC start/end (defaults to the camera's first and last event), automatic or manual bin size |
| KPI tiles | Vehicles counted, inbound vs outbound, busiest lane |
| Volume over time | One bar per bin, stacked by lane, with a toggle to split by direction. Hatched bars are partial: they start before the window or run past its end, so they cover less than a full bin and are left out of the peak |
| Lane comparison | Bars per lane split by direction: solid inbound, hatched outbound |
| Class breakdown | Vehicles per lane by class (car, truck, bus, motorcycle, bicycle) |
| Replay | Pick a video and its `_results.json`; boxes, track IDs, confidence, lane polygons, counting lines and running per-lane counts are drawn on a canvas over the `<video>` |
| Research access (footer) | Hidden until a link in the footer is used. Takes the research access code, keeps it in `sessionStorage` only, and lists the Tampa-area FL511 cameras from `GET /api/fl511/cameras`. A camera's snapshot loads only while that camera is selected, on demand through a "Refresh snapshot" button. FDOT confirmed on 2026-10-29 that the permission has no end date, so the panel no longer closes on its own |

Every data view has loading, empty and error states, and each chart has a "Show table"
alternative with a one-line summary, so the numbers are readable without seeing colour.
The layout works down to phone width (390 px, no horizontal scroll).

Missing bins are drawn as gaps, never as zeros: the API omits groups with no events, and
an empty window means "no recorded data", not "empty road". Bars are used rather than
smoothed curves so nothing is implied between bins, and bins that start before the window
or end after it are hatched as partial in the chart, the tooltip and the table.

## Florida research panel

The panel is opt-in behind an access code, and stays that way. FDOT's permission of
2026-10-29 allows public display, so where the Florida cameras eventually appear is a product
decision (Michael's), not a licence one — moving them onto the public map is part of the
redesign, not of this change.

- the access code lives in `sessionStorage`, never `localStorage`, so it dies with the tab;
- a `401` clears the code and says it was not accepted, a `503` says research access is not
  enabled on this deployment, and a `403` shows the API's own message;
- only the selected camera's `image_url` is ever loaded, and only on selection or on
  "Refresh snapshot" — there is no polling anywhere;
- `researchOpen()` is always true while `AUTHORIZED_THROUGH` is `null` (FDOT's permission of
  2026-10-29 has no end date). Set that constant to a date and the panel closes at the end of
  it, US Eastern — the rule is kept, it just has nothing to close on yet.

`src/lib/research.ts` holds those rules as pure functions and `src/lib/research.test.ts`
covers token storage, the status messages and the cutoff. The fixtures in
`src/mocks/index.ts` include three cameras with an inline placeholder image, so the panel
can be exercised offline without touching FL511. One mock camera, `caltrans-D3-8` (Hwy 65 at
Galleria), always answers `503 {"error_code": "stream_offline"}`, so the dead-stream state
and its **Try again** button can be seen without waiting for Caltrans to take a camera down.

## Replay details

The panel reads the file written by `pipeline/track_count.py --results-json`:

- frame index is `floor(video.currentTime * fps)`;
- boxes come from the nearest stored entry at or before that frame, because
  `boxes_every` can be greater than 1;
- box coordinates are scaled from `frame_size` to the displayed video size, and lane
  coordinates from the lanes file's own `frame_size`;
- a file whose `format` is not `traffic-analyzer-results/1` is rejected with a message.

Nothing is uploaded: both files are read locally with `URL.createObjectURL`.

## Screenshots

Taken against the local API with the sample camera in `frontend/docs/screenshots/`:

| File | View |
|------|------|
| `01-overview.jpg` | Controls, KPI tiles, start of the volume chart |
| `02-lane-and-class.jpg` | Lane comparison and class breakdown |
| `03-replay.jpg` | Replay with boxes, lane polygons, counting lines and per-lane counts |
| `04-phone.jpg` | Phone width (390 px) |
| `06-map.jpg` | The map over California, heat weighted by traffic level, legend bottom-left |
| `07-map-panel.jpg` | A selected camera: sweep result, then a running live analysis with the annotated frame and its numbers |
| `08-map-phone.jpg` | Phone width: the camera panel as a bottom sheet over the map |
| `09-history.jpg` | The History view, unchanged |

## Notes for whoever takes this over

- Recharts is the chart library (the team's choice in the root README). The bundle is
  about 630 kB before gzip because of it; splitting the replay panel into a lazy chunk
  is the obvious win if that matters.
- Bin sizes come from a fixed ladder in `src/lib/bins.ts`: the smallest rung that keeps
  the chart at or under 300 columns, which gives 10 s bins for a minute and 5 min bins
  for a day. The API's own limits (1–86400 s, 5000 bins) are wider.
- `binRows()` marks a row `partial` when `bin_start < start` or `bin_start + bin_seconds
  > end`; those bins cover less footage than the others, so they are hatched and excluded
  from the peak summary.
- Direction labels are per camera in `lanes.json` ("inbound"/"outbound" or "in"/"out");
  `directionKind()` maps both to inbound/outbound for the KPI split.
- The API caches data responses for 30 s; "Refresh data" refetches.
