---
title: Traffic Analyzer Worker
emoji: 🚦
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Traffic Analyzer worker

Turns one Caltrans camera into rows in the traffic database: record a few minutes of the
camera's HLS stream, run the tracking pipeline over it, import the events.

One job at a time, on demand, maximum ten minutes per recording. `camera_id` is checked
against `^D([1-9]|1[0-2])-\d{1,5}$` before it reaches a path, a source id or a command
line, and anything else is refused with 422 (so `../x`, an empty string and `D13-1` never
get that far). The worker does not learn
lanes by itself: a camera needs an approved `pipeline/lanes_caltrans_<camera>.json` in the
image, because an unattended `--auto-lanes` run can commit a layout nobody checked. FL511
is not handled here.

## Endpoints

| Method and path | What it does |
|-----------------|--------------|
| `GET /health` | Liveness, whether the environment is complete, and the last job's summary |
| `POST /jobs/record` | `{"camera_id": "D3-1", "minutes": 5}` with `Authorization: Bearer *** 202 when accepted, 409 while a job runs, 400 for a missing lanes file or more than ten minutes, 422 for a camera id that is not Caltrans-shaped (`^D([1-9]\|1[0-2])-\d{1,5}$`) |
| `GET /jobs/latest` | The last job's status, step, count and log tail (never secrets) |
| `POST /live/start` | `{"camera_id": "D3-41"}` with the same bearer token: 202 `{"session_id", "state": "starting"}`, or 409 `{"error": "another live analysis is running", "retry_after_s": N}`. 503 `{"error", "error_code"}` for a dead stream (`stream_offline`), an unreachable server (`stream_unreachable`) or a camera that cannot be analysed (`unavailable`). |
| `GET /live/{id}` | `state` (starting/running/done/failed), `camera_id`, `elapsed_s`, `level`, `flow_per_min`, `density`, `speed`, `detector`, `frame_seq`, `lanes`, `error`, `stopped_because` |
| `GET /live/{id}/frame.jpg` | The newest annotated frame, `image/jpeg`, `Cache-Control: no-store`; 404 before the first frame |
| `DELETE /live/{id}` | Stops the session |
| `GET /live/offline` | `{"cameras": ["D3-347", ...], "details": [{"camera_id", "error_code", "age_s"}]}` — cameras that failed in the last 15 minutes |

## Environment

| Variable | Meaning |
|----------|---------|
| `DATABASE_URL` | Postgres URL for the import. A writing login, not the API's read-only one. |
| `WORKER_TOKEN` | The bearer token every `/jobs` call must send. Spaces secret. |
| `DATA_DIR` | Where recordings and pipeline output go (default `/data`). |
| `WORKER_MODEL` | YOLO weights (default `yolo26n.pt`). |
| `WORKER_IMGSZ` | Model input size (default `640`). |
| `WORKER_SWEEP_DISTRICTS` | Comma-separated Caltrans districts to sweep, e.g. `3`. Unset or empty means no sweep. |
| `WORKER_DEVICE` | `cpu` (default) or a GPU index such as `0`. `run_local.ps1` sets `0`. |
| `WORKER_TEST_CLIP` | Test only: copy this local clip instead of recording a camera. |

The worker sets its own model and input size rather than changing the pipeline's defaults,
which were measured on a GPU. Measured on this laptop, `yolo26n` at 640 runs about 24 fps on
CPU; the tested defaults (`yolo26m`, 1280) are several times slower, which on free Space
hardware is the difference between minutes and an hour per recording.

## Running it locally

```bash
# the test clip the worker should "record" instead of a live camera
python - <<'PY'
import cv2
reader = cv2.VideoCapture("data/vehicles-2.mp4")
writer = cv2.VideoWriter("data/output/clip10.ts", cv2.VideoWriter_fourcc(*"mp4v"), 29.97, (1920, 1080))
for _ in range(300):
    ok, frame = reader.read()
    if not ok:
        break
    writer.write(frame)
print("wrote data/output/clip10.ts")
PY

docker build -t traffic-worker worker/
docker run --rm --network database_default -p 7860:7860 \
  -e DATABASE_URL=postgresql://traffic:traffic_dev@postgres:5432/traffic \
  -e WORKER_TOKEN=dev-token \
  -e WORKER_TEST_CLIP=/data/clip10.ts \
  -v "$PWD/data/output:/data" \
  traffic-worker

curl -s -X POST localhost:7860/jobs/record -H 'Authorization: Bearer dev-token' \
  -H 'content-type: application/json' -d '{"camera_id": "D3-1", "minutes": 0.2}'
curl -s localhost:7860/jobs/latest -H 'Authorization: Bearer dev-token'
```

The test clip stands in for a live recording so nothing talks to Caltrans. For a camera the
worker can track, the image needs that camera's lanes file. The smoke test used a
`pipeline/lanes_caltrans_D3-1.json` copied from `pipeline/lanes_vehicles-2.json` — a local
test file that matches the sample video's view and was **never committed**, so a fresh
checkout has no `D3-1` lanes and a `D3-1` job is refused until one exists.

## A camera that is listed but not streaming

Caltrans keeps dead cameras in its district feed, so `POST /live/start` makes one small GET
of the camera's playlist before opening a capture:

| what came back | `error_code` | what a person is told |
|---|---|---|
| 404 or 410 | `stream_offline` | Caltrans isn't serving video for this camera right now. |
| 200 with an empty body, or something that is not a playlist | `stream_offline` | the same |
| a connection error or a timeout | `stream_unreachable` | The stream server could not be reached. Try again in a minute. |
| anything else that is not 2xx | `stream_unreachable` | the same |

A camera that fails is remembered for 15 minutes: another start inside that window answers
503 with the same code without touching the network, and `GET /live/offline` lists them.
The exception type and anything a decoder said never reach the panel — a failed session
carries `error_code` and one sentence.

## Live analysis

A map click asks the worker to analyse one camera's own stream and say how much traffic is on
the road. Browsers cannot load Caltrans streams (the stream server refuses a foreign
`Origin`), so the panel shows annotated JPEGs produced here instead.

- **One session at a time**, at most two minutes, and it stops **30 s after the last poll** —
  a closed tab does not keep the GPU busy. Nothing is written to disk.
- The stream is read with OpenCV, tracked with the same BYTETracker as the counting pipeline,
  and judged every 10 s by `pipeline/traffic_level.py`, which is the single definition of
  "heavy" shared with the sweep.
- **Detector:** the sun at the camera decides first (`pipeline/daylight.py`, from the
  camera's own latitude and longitude): ordinary YOLO between sunrise and sunset, headlight
  mode after civil dusk. Only in civil twilight (about 25 minutes either side), or for a
  camera without coordinates, does the road's brightness decide. Brightness is the *median*
  pixel of the middle of the frame, not the mean: headlights, bridge lamps and the burned-in
  name plate drag the mean up, and Caltrans D3-52 averages 55 while its road is 38. In
  twilight the choice keeps a margin either side of the threshold, so a camera near the line
  does not flip detector frame by frame. Each status carries
  `light_source: {phase, brightness, mode}` saying why.
- **Lanes:** the camera's committed `pipeline/lanes_caltrans_<id>.json` if there is one,
  otherwise a road-level line across the middle of the frame and the whole frame for
  density. The response says `lanes: "lanes file"` or `"road"` so nobody mistakes the
  fallback for lane-level counting.

On the recorded D3-52 night clip (68 s, 640x480, CPU): headlights selected, 309 frames
analysed, `level: moderate`, flow 132/min, density 5.4, speed 4.19, and the annotated frame
shows tracked headlights with ids over the road polygon and counting line.

## The 30-minute sweep

The map's heat layer needs every camera in a district, not just the one somebody clicked, so
the worker sweeps on a schedule. It starts inside the worker process when
`WORKER_SWEEP_DISTRICTS` is set (`run_local.ps1` sets `3`).

- **Catalogue:** every camera in the configured districts is upserted into `traffic.cameras`
  once a day, from the same Caltrans feeds the recorder uses. Each row carries its still URL.
- **Every 30 minutes:** eight stills per camera, fetched about 60 s apart with at most two
  requests in flight, and an identical still (the feed has not refreshed yet) is not counted
  twice. Each still is detected at tiled 640 and the results become one row per camera in
  `traffic.camera_sweeps`: `frames`, `vehicles_mean`, `vehicles_max`, `mean_brightness`,
  `level`, `detector`.
- **The level** comes from `pipeline/traffic_level.py`, the same module live analysis uses:
  a camera is judged against its own last 60 sweeps, at or above its 80th percentile is
  `heavy`, the 40th to 80th `moderate`, below that `light`, and fewer than eight sweeps is
  `learning`.
- **Day and night come from the sun** at each camera, not from brightness alone. After
  civil dusk a sweep is `night` however bright the road (a lit interchange at 2 am is still
  night), and night sweeps are left out of the history a daytime sweep is judged against.
  Between sunrise and sunset a dark road is a camera problem, not night: the sweep is
  `unknown`, the camera is listed in the status's `dark_in_daylight`, and it is logged. In
  civil twilight the brightness rule decides, as before. Counts and brightness are stored
  either way.
- **A failed camera** is logged and skipped for the rest of that sweep, not retried; one dead
  feed does not stop the round.
- **Sleep and wake:** the next sweep is scheduled from when the last one *finished*. A PC
  that slept through three hours runs one sweep on waking, not six.
- `GET /sweep/status` (bearer token) reports `running`, the districts, the last start and
  finish, the next run, and counts of cameras, swept, failed and duplicate stills. `/health`
  carries the same block.

Measured against the local database on 2026-10-29: the district-3 catalogue upserted **265
cameras in 2.1 s**, and a three-camera sweep wrote three rows in 5.9 s with three duplicate
stills skipped, giving `night` for the two dark cameras (brightness 30.5 and 17.0, no
detections) and `learning` for the one with a vehicle. `traffic.camera_map()` returns them
with `swept = true` and their levels.

## The camera view check

`track_count.py` runs with `--view-check strict`, so a camera whose view has moved since its
lanes were learned fails the job instead of importing counts that no longer fit the road.
The job's `error` reads `camera view changed: re-learn the lanes (<details>)`, and the log
tail carries the check's own line (`camera view check: moved ... ; shift ... px; scale ...`).
Lanes files without a reference image skip the check and the job runs as before; a night
frame against a daytime reference comes back `unknown`, which also does not stop the job.

## Deploying

The worker runs on Michael's PC with his GPU (`worker/run_local.ps1`) and writes to Neon
through the insert-only `traffic_writer` login. The Docker image is kept for a future hosted
deployment if the team pays for one; it needs the repo root as its build context:

```bash
docker build -f worker/Dockerfile -t traffic-worker .
```

and takes `DATABASE_URL`, `WORKER_TOKEN` and `HF_TOKEN` (if the model repo is private) as
environment variables or Space secrets. Free CPU hardware only; do not enable paid GPU
hardware without Michael's decision.

## Reaching the worker from Vercel (Cloudflare Tunnel)

The worker only listens on `127.0.0.1:7860`. The deployed API reaches it through a named
Cloudflare Tunnel, `traffic-worker`, at `https://worker.brightwaterts.com`. No port is opened on
the router; `cloudflared` dials out to Cloudflare. Every route except `/health` still needs the
worker token, which only the API has (Vercel env `WORKER_TOKEN`), so browsers never see it.

One-time setup (already done on Michael's PC):

```powershell
cloudflared tunnel login
cloudflared tunnel create traffic-worker
cloudflared tunnel route dns traffic-worker worker.brightwaterts.com
```

`%USERPROFILE%\.cloudflared\config.yml` (the credentials JSON next to it is a secret; never
commit it):

```yaml
tunnel: <tunnel id>
credentials-file: C:\Users\<you>\.cloudflared\<tunnel id>.json
ingress:
  - hostname: worker.brightwaterts.com
    service: http://127.0.0.1:7860
  - service: http_status:404
```

Each session, in two terminals: `.\worker\run_local.ps1`, then
`cloudflared tunnel run traffic-worker`. Check it with
`curl https://worker.brightwaterts.com/health`. When either is stopped, the map keeps working
from stored sweeps and live analysis answers 503 "live analysis is offline".

Vercel needs `WORKER_URL=https://worker.brightwaterts.com` and the same `WORKER_TOKEN` that
`run_local.ps1` was given (and optionally `LIVE_ACCESS_CODE`).
