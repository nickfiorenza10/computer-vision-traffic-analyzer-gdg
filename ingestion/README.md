# Live-camera ingestion

Pulls footage from public traffic cameras so the [pipeline](../pipeline/README.md)
has real input. Two sources are supported:

| Source | What it gives | Use it for |
|--------|---------------|------------|
| [Caltrans CCTV](#caltrans-live-video) (California) | Live video, 640×480 at 15 fps | Tracking and lane counting |
| [FL511](#fl511-snapshots) (Florida, Tampa area) | A still image every 1–2 minutes | Checking camera views, single-image detection |

Tracking needs continuous frames, so **use Caltrans for counting**. FL511
publishes snapshots only; its live video sits behind short-lived tokens issued
to fl511.com's own player, and its terms limit content to individual use
without FDOT's written consent (see [Terms](#terms)).

Run every command from the repository root with Python 3.13. The module uses
only the standard library. Output goes to `data/live_footage/`, which is
git-ignored; pass `--output` to choose another directory.

## Caltrans live video

The [CWWP2 CCTV feed](https://cwwp2.dot.ca.gov/closed-circuit-television-cameras.html)
lists Caltrans cameras by district (1–12). About 2,000 of them have a public
HLS video stream: H.264 video delivered in 10-second segments.

### Find cameras

```powershell
python -m ingestion caltrans-list --district 3 --county Sacramento
```

Only cameras that are in service and have a stream are listed. Omit
`--district` to list all 12 districts (one request each); repeat it to list
several. Camera IDs look like `D3-1`: the district plus the camera's index in
that district's feed.

Most cameras are on freeways, and operators can pan or zoom them, so check the
view before choosing one. A moved camera needs new lanes.

### Record video

```powershell
python -m ingestion caltrans-record --camera-id D3-1 --minutes 10
```

The recorder reads the stream's playlist, picks the highest-quality variant,
and starts at the newest segment. It then re-checks the playlist every few
seconds and appends each new segment, unchanged, to one file until the
requested length is saved. Ctrl+C stops it early; the partial `.ts` file is
kept but not added to the manifest.

Each recording produces:

- `data/live_footage/caltrans_<ID>/<UTC start>.ts`: the video (MPEG-TS, which
  OpenCV reads like any other video file).
- One line in `data/live_footage/manifest.jsonl`:

| Field | Meaning |
|-------|---------|
| `started_at` | UTC time the first segment arrived. The video starts about 10 seconds earlier. |
| `video` | Path to the `.ts` file, relative to the output directory. |
| `recorded_seconds` | Length of video saved. |
| `segments` | Number of 10-second segments saved. |
| `missed_segments` | Segments that left the playlist before they were downloaded. Should be `0`. |
| `camera_id`, `location`, `county`, `route`, … | Camera details from the feed. |

### Run the pipeline on a recording

Pass the manifest's `started_at` as `--start`. Without it, `track_count.py`
uses the file's modified time, which is when recording *ended*.

Each camera needs its own lanes file. Without `--lanes`, `track_count.py` uses
`pipeline/lanes.json`, which belongs to a different camera. The first time,
add `--auto-lanes` to learn lanes from the recording and save them, then check
`data/output/<recording>_lanes.jpg`. Later recordings from the same camera view
can reuse the file without `--auto-lanes`:

```powershell
python pipeline/track_count.py data/live_footage/caltrans_D3-1/20261027T005520Z.ts --start 2026-10-27T00:55:21+00:00 --auto-lanes --lanes pipeline/lanes_caltrans_D3-1.json
```

If `missed_segments` is above zero, the stream skipped ahead during recording.
Timestamps after that point come out early by about 10 seconds per missed
segment.

## FL511 snapshots

The [FL511_Traffic_Cameras FeatureServer](https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services/FL511_Traffic_Cameras/FeatureServer)
is a **camera metadata and still-image source**, not a video-stream API. Each
camera record has an `IMAGE` URL, which FL511 replaces roughly every one to two
minutes. The layer's `TIMESTAMP` may lag behind the image itself, so
`captured_at` in the manifest records when this process retrieved the image.

### Find cameras

```powershell
python -m ingestion list
```

By default, the query is limited to Hillsborough County and this Tampa-area
bounding box:

```text
west=-82.60, south=27.80, east=-82.30, north=28.10
```

Limited-access highways, ramps, and interchanges are omitted by default to
prioritize surface-road cameras. Include them with `--include-highways`, or
adjust the bounds with `--bbox west,south,east,north`.

Potential urban-intersection candidates in the current layer include:

- `1043` — Dale Mabry Hwy at Hillsborough Ave
- `1038` — Hillsborough Ave at Himes Ave
- `841` — Dale Mabry Hwy at Lemon St
- `838` — Himes Ave at Lemon St
- `757` — camera described as “at 6th Ave” in the Ybor area

These are candidates based on the camera metadata, not a guarantee of
pedestrian visibility or useful framing. Review the view manually before
choosing a camera; the layer can change.

### Capture snapshots

Capture one image from a camera:

```powershell
python -m ingestion capture --camera-id 1043 --once
```

Poll selected cameras every 30 seconds until stopped with Ctrl+C:

```powershell
python -m ingestion capture --camera-id 1043 --camera-id 1038 --interval-seconds 30
```

Snapshots are written to `data/live_footage/camera_<ID>/`, and each successful
download is recorded in `data/live_footage/manifest.jsonl`. Polling faster than
FL511 updates the image only saves duplicates.

Each snapshot can be used as an image input for detection. It cannot drive
tracking or lane counting: the pipeline expects continuous frames from one
source, and a snapshot every minute or two is not that.

## FL511 live video (authorized research only)

FL511 serves live video only to its own page, with short-lived tokens. There is **no
per-camera page address**: `?camera=<id>`, `#camera-<id>` and `/cctv/<id>` were all
checked in a browser and none opens a camera. The player is the camera list at
`/cctv`, and a camera is opened by clicking its card, which makes three requests that
the recorder makes directly instead of driving a browser:

| request | gives |
|---|---|
| `GET /tooltip/Cameras/<id>?lang=en&noCss=true` | the camera's `data-videourl`, the base playlist on `dis-*.divas.cloud` |
| `GET /Camera/GetVideoUrl?imageId=<id>` | `{token, sourceId, systemSourceId}` |
| `POST https://divas.cloud/VDS-API/SecureTokenUri/GetSecureTokenUriBySourceId` | the `?token=...` suffix the player appends |

The playlist only answers with `Referer: https://fl511.com/cctv`, the header that page
sends; without it the stream is a 401. FL511's stream is **fragmented MP4** behind
`#EXT-X-MAP`, so the file is written with its initialisation segment first and named
`.mp4`. Recording then uses the same code as the Caltrans recorder (`ingestion/hls.py`),
which learned to write both containers.

**It refuses to run until FDOT's written confirmation is on record.** One switch in
`ingestion/fl511_video.py` controls it:

```python
FDOT_VIDEO_CONFIRMED_ON: str | None = None   # None = capture refused
```

Only Claude sets it, in its own commit, on Michael's word. There is no flag and no
environment variable that overrides it.

**Known FL511 issue (2026-10-29).** Camera **1267**'s snapshot and its video are different
sources. `GET https://fl511.com/tooltip/Cameras/1267?lang=en&noCss=true` returns
`data-camera-id="1267"` with
`data-videourl=https://dis-se13.divas.cloud:8200/chan-1051_h/index.m3u8`, and a recording of
that stream is burned "**I-10 WB at MM351.6 W of Chaffee**" — a different road, 200 miles from
Gandy Boulevard. The snapshot (`chan-11034_h`) is the right camera. Camera 1051's own tooltip
points at `chan-9376_h`, so it is not an off-by-one: FL511 is publishing the wrong video for
this camera. Do not point live analysis at Florida streams until the mapping is worked out;
Florida has no live analysis yet.

FDOT confirmed by email on **2026-10-29** that the project may use FL511 with **no end
date, public display included**. That replaced the earlier 2026-12-01 limit. `EXPIRY_DATE` in
`ingestion/fl511_video.py` is therefore `None`; the check still refuses on or after a date if
one is ever set again. The other limits stand: no camera control, no restricted FDOT systems,
and no CAPTCHA bypassing or disguising automation.

```bash
python -m ingestion fl511-record --camera-id 2247 --minutes 1
```

The recorder keeps these rules: no disguising automation (stock user agent, no stealth
plugins), on a CAPTCHA, bot check, login, or a 403/429 it stops and says so instead of
retrying or taking another route, one camera per process, at most two recordings at once
(lock files in `data/live_footage/`), and ten minutes per recording. Tokens expire
mid-recording, so it resolves a fresh playlist URL at most once per 60 seconds and at
most five times per recording, then stops and keeps what it has.

`open_player_playlist` remains as a browser fallback (Playwright, install with
`pip install -r ingestion/requirements-video.txt` and `playwright install chromium`) and
`--player-url` points it at a page that already lists the camera's card, but it is no
longer the default: the list is paginated, so a page cannot be aimed at an arbitrary
camera.

The stream's timestamps are continuous from the encoder, so a recording's own duration reads
as the stream's uptime rather than its length: the 1-minute test recording claimed 50,162 s
(13.9 h). After recording, the file is remuxed once with
`ffmpeg -c copy -reset_timestamps 1` — no re-encoding, about a second per minute of video — so
the same file reports 62.46 s. The raw recording is kept.

Output is `data/live_footage/fl511_<id>/<UTC start>.mp4` (the remuxed one, named
`.timed.mp4`) plus a line in `manifest.jsonl` with `"source": "fl511-video"`, the segment
counts, `refreshes` and `stopped_because`. The manifest's `"video"` is the file to use,
`"video_raw"` is the untouched recording, and `"timestamps"` says which of the two you have —
if ffmpeg is not on the machine the recording stands as recorded and the manifest says so. A
recording that fails part way is deleted rather than left as an unplayable stub, and written
to no manifest.
`started_at` comes from `EXT-X-PROGRAM-DATE-TIME` when the playlist carries it, otherwise
from when the first segment arrived, exactly as the Caltrans recorder does.

## Terms

Public visibility does not by itself grant permission for automated capture,
redistribution, or AI processing.

- **Caltrans:** the CCTV data is offered "for integration into your
  application" at no charge. Its fair-use rule: "Usage that risks degrading
  the availability of the CCTV streaming service is prohibited." Record a few
  cameras at a time, not dozens around the clock. The terms don't address
  republishing video; ask the CWWP2 contact before showing it publicly.
- **FL511:** content is "for individual use only and is not available for
  re-sale or re-use without the express written consent of FDOT"
  ([conditions](https://fl511.com/privacy)). Get FDOT's consent before
  sustained ingestion; [fl511.com/contact](https://fl511.com/contact) is the
  starting point.
