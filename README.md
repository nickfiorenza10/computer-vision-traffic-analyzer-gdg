# Computer Vision Traffic Analyzer

> **Portfolio copy.** This is a public copy, shared with the team's permission, of a team project built in the Google Developer Groups (GDG) at USF mentorship program. The team's original repository is private.
>
> **Team:** Daksh Talati (team lead), Monisha Natarajan (backend), Sebastian Ward (database), Michael McGrogan (full stack), ChrisAnn Wright (frontend), and Nicholas Fiorenza (testing). Full roles: [TEAM.md](TEAM.md).
>
> **My role (Nicholas Fiorenza, testing):** validated the pipeline's automated vehicle counts against manual counts across 20+ videos, tested detection in night, rain, and occluded-vehicle conditions and reported issues to the developers, and merged a pull request adding a JSON results export to the YOLO pipeline and a front-end dashboard MVP mockup.

A GDG project that detects, tracks, and counts vehicles in traffic video, day or night, then shows lane-level flow and congestion on a web dashboard. Its main page is a live map of California's Caltrans cameras: click a road and the camera is analysed live. Team roles: [TEAM.md](TEAM.md).

## Goals
- Measure vehicle volume by class and lane over time
- Identify congestion periods using [metric]
- Validate automated counts against manual counts (target: ±X%)

## How It Works
`Video → Python (YOLO detect + track) → events.csv → PostgreSQL → Rust API → React dashboard`

```
Browser ─► Vercel ─┬─ React dashboard (map + History)
                   └─ /api/* Rust function ─┬─► Neon PostgreSQL (read-only login)
                                            └─► worker, through a Cloudflare Tunnel
Worker (a PC with an NVIDIA GPU) ─┬─ every 30 min: 8 stills per camera → density level → Neon
                                  └─ on a click: live video → tracking → level + annotated frame
```

- **Dashboard and API:** hosted on [Vercel](https://vercel.com/). The API is a Vercel Rust function ([api/README.md](api/README.md)). Deployment steps: [DEPLOY.md](DEPLOY.md).
- **Map:** every Caltrans camera, coloured by its latest traffic level (heavy, moderate, light), or by camera coverage. Clicking a camera opens a panel with its still, its last sweep, and live analysis ([frontend/README.md](frontend/README.md)).
- **Worker:** runs on Michael's PC with the GPU ([worker/README.md](worker/README.md)). The 30-minute sweep covers district 3 (Sacramento) for now. Live analysis runs one session at a time, for up to two minutes. The API reaches the worker at `worker.brightwaterts.com` through a Cloudflare Tunnel, so no port is opened on the PC.
- **Traffic level:** flow and density, defined once in `pipeline/traffic_level.py`. Live analysis judges flow, density and a speed proxy. The sweep judges each camera's density against its own history, labelling a camera `learning` until it has 8 sweeps and `night` after dusk at that camera.
- **Day and night:** sunrise, sunset and civil twilight are computed for every camera from its own coordinates (`pipeline/daylight.py`, within a minute of the US Naval Observatory), for California and Florida alike. In daylight the pipeline uses YOLO; after dusk it switches to headlight mode (`pipeline/headlights.py`), which tracks headlight pairs instead of cars. Lanes are learned from daytime footage, and a view check (`pipeline/view_check.py`) refuses to count if the camera has been moved since.
- **Input:** a video file, or live camera video recorded by [ingestion](ingestion/README.md): Caltrans video, and FL511 (Florida) video and snapshots for research.
- **Storage:** PostgreSQL sits between the CSV output and the API. See the [database setup and API handoff](database/README.md) for Sebastian Ward's schema, CSV loader, lane totals and time-bin queries. The map's tables are in `database/map_schema.sql`.

To show a video's YOLO boxes, track IDs and lane counts on it, run `pipeline/track_count.py` with `--results-json` (see the [pipeline README](pipeline/README.md)).

**Florida (FL511)** is used under FDOT's written permission. As of 2026-10-29 that permission has no end date and covers public display. The Florida cameras are public, next to California on the map.

## Requirements
- **Python 3.13**: detection and tracking pipeline
- **Rust (stable)**: API (`api/`, `src/`); builds can run in Docker, see [api/README.md](api/README.md)
- **Node.js LTS**: React dashboard (`frontend/`)
- Python environment setup: see [SETUP.md](SETUP.md)

## Tech Stack
| Part | Tools |
|------|-------|
| Detection | Python, [Ultralytics YOLO](https://docs.ultralytics.com/) (pretrained COCO model), OpenCV, PyTorch |
| Tracking & counting | `model.track()` (ByteTrack / BoT-SORT), [Supervision](https://supervision.roboflow.com/), pandas |
| API | Rust, [Axum](https://github.com/tokio-rs/axum), [sqlx](https://github.com/launchbadge/sqlx), deployed as a [Vercel Rust function](https://vercel.com/docs/functions/runtimes/rust) |
| Night | Headlight tracking: OpenCV background model + ByteTrack (`pipeline/headlights.py`) |
| Frontend | [React](https://react.dev/) + Vite, [MapLibre GL](https://maplibre.org/) with [OpenFreeMap](https://openfreemap.org/) tiles, Recharts, hosted on Vercel |
| Database | PostgreSQL on [Neon](https://neon.tech/) |
| Worker | Python, [FastAPI](https://fastapi.tiangolo.com/), on a local NVIDIA GPU, reached through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) |
| Model weights | Ultralytics YOLO26; loading from [Hugging Face](https://huggingface.co/) is supported (`pipeline/model_source.py`) |
| GPU | A local NVIDIA RTX 3060 (Michael's PC) runs the pipeline, the sweep and live analysis |
| Team tools | Git & GitHub |

## events.csv Schema
| Column | Description |
|--------|-------------|
| `timestamp` | Real-world time (clip start + frame / FPS) |
| `frame` | Frame index |
| `track_id` | Unique ID from the tracker |
| `class` | car, truck, bus, motorcycle, bicycle |
| `lane` | Lane ID from `lanes.json` |
| `direction` | e.g. inbound / outbound |

One row is one vehicle's first counted crossing within a source video. The header
order above is required. Tracker IDs are unique within a source, and lane IDs are
local to a camera's lane layout. Include a UTC offset in the pipeline's `--start`
or supply `--naive-offset` when importing timestamps without one. The
[full storage contract](database/README.md#csv-contract-and-database-mapping)
defines validation, source identity, and database column mappings.

## Project Structure
```
data/          # raw videos, live_footage/ recordings, output CSVs (git-ignored)
ingestion/     # Caltrans live video, FL511 (Florida) video and snapshots
pipeline/      # Python: detect, track_count, auto_lanes, headlights, view_check, traffic_level
database/      # PostgreSQL schema, map tables, login roles, CSV importer, queries, tests
worker/        # GPU worker: recording jobs, 30-minute sweep, live analysis; run_local.ps1
api/, src/     # Rust API (Vercel function): counts, map GeoJSON, live proxy; Cargo.toml at the root
frontend/      # React dashboard: camera map, History charts, Florida research panel
```

## Limitations
- Night counts come from headlights. They are less precise than daytime YOLO and can pick up static glare. Lanes must be learned in daylight first.
- Accuracy still drops in rain and with heavy occlusion.
- Lane assignment depends on a fixed camera view. Caltrans operators can pan and zoom, so a moved camera needs new lanes; the view check catches this.
- The sweep reads stills, not video. After dusk it labels a camera `night` instead of guessing, and a camera that is dark in daylight `unknown`.
- Live analysis and the sweep only run while the worker PC is on. When it's off, the map shows the last stored sweep and live analysis reports "offline".
- Caltrans streams sometimes go offline even when the feed lists them.

Reference: [Object Detection and Tracking with YOLOv8](https://www.linkedin.com/posts/object-detection-and-tracking-with-yolov8-share-7072888917141651456-cG_O/)
