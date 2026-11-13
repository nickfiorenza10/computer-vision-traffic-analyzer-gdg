# Pipeline

Turns a traffic video into `events.csv`: one row per vehicle, logged when it crosses a lane's counting line. Columns match the [events.csv schema](../README.md#eventscsv-schema).

```
video → YOLO detect → ByteTrack track → lane polygons + counting lines → events.csv
```

Set up the Python environment first ([SETUP.md](../SETUP.md)), activate it, and run everything from the project root.

## Scripts

| Script | What it does |
|--------|--------------|
| `detect.py` | Detection only. Saves an annotated video to check that YOLO finds the vehicles. |
| `auto_lanes.py` | Learns lanes from vehicle paths and writes a lanes file plus a preview image. |
| `track_count.py` | Tracks vehicles, counts lane-line crossings, and writes `events.csv`. |
| `hand_count.py` | Plays a video with the lanes drawn on it; you press a lane's number when a vehicle crosses. Writes a hand-count CSV. |
| `compare_counts.py` | Compares a hand count with `events.csv`: matched, missed and extra vehicles per lane. |

## Quick start

1. Put a video in `data/` (videos are git-ignored), or record live camera video with [ingestion](../ingestion/README.md#caltrans-live-video).
2. Make a lanes file for the camera (see [Lanes](#lanes)):

   ```bash
   python pipeline/auto_lanes.py data/my_camera.mp4 --output pipeline/lanes_my_camera.json
   ```

3. Open `data/output/my_camera_lanes.jpg` and check that the lanes match the road.
4. Count:

   ```bash
   python pipeline/track_count.py data/my_camera.mp4 --lanes pipeline/lanes_my_camera.json --start 2026-10-09T08:00:00 --save-video
   ```

Output goes to `data/output/` (git-ignored): `my_camera_events.csv` and, with `--save-video`, `my_camera_tracked.mp4`.

To view a video on the dashboard with YOLO's boxes, track IDs and lane counts, add `--results-json`, then load the video and `data/output/my_camera_results.json` in the dashboard's Live camera panel:

```bash
python pipeline/track_count.py data/my_camera.mp4 --lanes pipeline/lanes_my_camera.json --start 2026-10-09T08:00:00 --results-json
```

## Common options

| Option | Scripts | Default | Notes |
|--------|---------|---------|-------|
| `--start` | track_count | file's modified time | Real-world clip start, e.g. `2026-10-09T08:00:00`. Always set it; timestamps are `start + frame / fps`. |
| `--lanes` | track_count | `pipeline/lanes.json` | Lanes file for this camera. |
| `--auto-lanes` | track_count | off | Run `auto_lanes.py` first and save to `--lanes`. |
| `--device` | all | GPU if available | `cpu` to force CPU. Don't hardcode a device in code. |
| `--model` | all | `yolo26m.pt` | Any Ultralytics weights; downloads automatically. `hf:<owner>/<repo>/<file>` downloads from the Hugging Face Hub (`HF_TOKEN` for a private repo) and behaves exactly like a local path. See [Model settings](#model-settings). |
| `--tracker` | track_count, auto_lanes | `bytetrack.yaml` | Or `botsort.yaml`. |
| `--detector` | track_count, auto_lanes | `yolo` | `headlights` for dark night video where only lights are visible. See [Night: headlight mode](#night-headlight-mode). |
| `--conf` | all | `0.2` | Minimum detection confidence. |
| `--imgsz` | all | `1280` | Size YOLO resizes frames to. Smaller is faster but misses distant vehicles. |
| `--save-video` / `--show` | track_count | off | Annotated output video / live preview (press q). |
| `--results-json` | track_count | off | Also write `<name>_results.json` for the web dashboard: YOLO boxes with track IDs, classes and confidence per frame, plus the lanes and counting events. |
| `--results-every` | track_count | `1` | With `--results-json`, keep boxes for every Nth frame (2 or 3 shrinks the file for long videos). |
| `--seconds` | auto_lanes | `60` | How much video to learn lanes from. |
| `--min-tracks` | auto_lanes | `2` | Vehicles needed before a lane is accepted. |

## Lanes

Each camera needs its own lanes file. A lane has a `polygon` (the lane area), a counting `line`, and `directions` that name each crossing direction:

```json
{
  "frame_size": [3840, 2160],
  "lanes": [
    {
      "id": 1,
      "polygon": [[1422, 800], [1520, 800], [942, 2160], [160, 2160]],
      "line": [[866, 1400], [1266, 1400]],
      "directions": {"in": "outbound", "out": "inbound"}
    }
  ]
}
```

- **Resolution:** coordinates are scaled from `frame_size` to the video, so one file works for the 4K original and a 1080p copy. A different aspect ratio prints a warning.
- **Direction:** with a line drawn left to right, moving up the frame is `in` and moving down is `out`. We call moving down (toward the camera) `inbound`. If directions come out backwards, swap the names.
- **Automatic lanes** (`auto_lanes.py`) work well on busy footage: each lane shows up as a peak in where vehicles cross the counting line. A lane is only found if several vehicles drive in it, so on a quiet camera use a longer clip or fix the file by hand. Always check the preview image. It assumes roughly straight lanes with traffic moving up/down or left/right in the frame.
- **By hand:** grab a frame and click out points with the [PolygonZone tool](https://polygonzone.roboflow.com/).

| File | Camera |
|------|--------|
| `lanes.json` | supervision sample `vehicles.mp4` (hand-drawn, 4 lanes) |
| `lanes_vehicles-2.json` | supervision sample `vehicles-2.mp4` (auto-generated with the default settings, 6 lanes) |

Both samples download from `https://media.roboflow.com/supervision/video-examples/<name>`.

## How counting works

- A vehicle is logged once, the first time its bottom-center point crosses a lane's line while inside that lane's polygon.
- Each vehicle's `class` is the one YOLO assigned most often over its track, so a truck briefly labeled "bus" stays a truck.
- Events are logged a couple of frames after the actual crossing, because the tracker confirms a vehicle first.

## Model settings

The defaults (`yolo26m.pt`, `--imgsz 1280`, `--conf 0.2`) were picked by comparing settings on `vehicles-2.mp4` against hand counts:

| Model, imgsz, conf | Lane 1, frames 0-110 (12 by hand) | Lane 4, first 2 s (8 by hand) | Time for the 42 s clip on an RTX 3060 |
|---|---|---|---|
| n, 640, 0.3 (old default) | 2 | 7 | 78 s |
| s, 640, 0.3 | 6 | 8 | 67 s |
| m, 640, 0.3 | 3 | 8 | 64 s |
| s, 1280, 0.2 | 12 + 1 extra | 7 | 70 s |
| **m, 1280, 0.2** | **12** | **8** | 111 s |

The input size matters most: at 640 the frame is shrunk so much that distant vehicles are missed, and with them whole lanes. On a slow GPU or CPU, `--model yolo26s.pt` is the next best choice, and `--imgsz 640` trades accuracy for speed.

## Measuring accuracy

To measure how accurate the pipeline is, count a clip by hand and compare.

1. Count by hand, with the same lanes file the pipeline used:

   ```bash
   python pipeline/hand_count.py data/vehicles-2.mp4 --lanes pipeline/lanes_vehicles-2.json
   ```

   Press **1–9** when a vehicle's front crosses that lane's red line. **Space** pauses,
   **a**/**d** step one frame, **[**/**]** change speed, **u** undoes the last count,
   **s** saves, and **q** saves and quits. Slow playback (0.25x–0.5x) works best for busy
   lanes. Counts go to `data/output/<video>_hand.csv`, and a `.json` next to it records
   the frames you reviewed. Run the same command again to pick up where you left off.
   `--start-frame` and `--end-frame` limit the session to part of a video.

2. Compare it with the pipeline's output for the same video:

   ```bash
   python pipeline/compare_counts.py data/output/vehicles-2_hand.csv data/output/vehicles-2_events.csv
   ```

   Only the frames you reviewed are compared. Each pipeline event is matched to a hand
   count in the same lane within 0.5 s (`--tolerance`). The report shows, per lane:
   **missed** (counted by hand only), **extra** (counted by the pipeline only, e.g. double
   counts), count error, recall and precision, plus the mean error per 10 s bin. If you
   counted only some lanes, pass them with `--lanes 1 2`.

Count error alone can hide mistakes, because a missed car and a double count cancel
out. Recall and precision don't.

Example, using a hand count of lane 1 for the first 3.7 s of `vehicles-2.mp4`:

| Settings | Hand | Pipeline | Matched | Recall | Precision |
|----------|------|----------|---------|--------|-----------|
| `yolo26n`, 640 (old default) | 12 | 2 | 2 | 16.7% | 100% |
| `yolo26m`, 1280 (default) | 12 | 12 | 12 | 100% | 100% |

## Sample output

[`data/sample_events.csv`](../data/sample_events.csv) is real output from `vehicles-2.mp4` (42 s, 787 vehicles, 6 lanes) with the default settings, for building the database and API against. The timestamps are not real: the clip start was set to `2026-10-09T08:00:00`.

## Day, night and the sun

`daylight.py` computes civil dawn, sunrise, sunset and civil dusk for any latitude and
longitude, offline (the sunrise equation; within a minute of the US Naval Observatory for
Sacramento and Tampa, checked in `tests/test_daylight.py`). `phase(lat, lon, when)` answers
`day`, `twilight` or `night`, and `traffic_level.lighting(phase, brightness)` turns that into
the detector choice: the sun settles day and night, brightness decides only in twilight.

```powershell
python pipeline/daylight.py 38.58 -121.49    # Sacramento: today's times and the phase now
```

Daytime-only work (learning lanes, precision snapshots) should check
`daylight.daylight_wait(lat, lon, now)` first: it returns None in daylight, or a sentence
naming the next sunrise. FL511 refreshes a still about every three minutes, so capture
FL511 stills 180 s apart.

## Night: headlight mode

In the dark, traffic cameras switch to a black-and-white night mode and vehicles become
pairs of headlights on a black road. The COCO-trained YOLO model finds nothing there: on
a dark Caltrans clip it detected **zero** vehicles, even at a 0.05 confidence cutoff.
`--detector headlights` (`pipeline/headlights.py`) counts them from their lights:

1. A background model learns what doesn't move (street lights, signs, the camera's text
   overlay).
2. Pixels that are both very bright (a headlight core) and moving are kept, and a
   car's two headlights are joined into one blob.
3. Each blob becomes a vehicle-sized box, grown upward from the lights so the tracker can
   follow it between frames. The bottom edge stays at the lights, where lanes and
   counting lines look.
4. The same BYTETracker, lanes, counting lines and `events.csv` as daytime take it from
   there, so the database, API and dashboard work unchanged.

```bash
python pipeline/track_count.py data/live_footage/caltrans_D3-52/<recording>.ts --detector headlights --start <manifest started_at> --auto-lanes --auto-seconds 180 --lanes pipeline/lanes_caltrans_D3-52.json --save-video
```

`--auto-lanes` works in headlight mode too: lanes are learned from headlight paths, so a
dark camera needs no hand-drawn lanes. It runs on CPU at about 100 FPS on 640×480
Caltrans video, which is several times real time, so no GPU is needed.

**First results (Caltrans D3-52, I-80 at Bryte Bend, 00:27 PDT, 3 minutes):** YOLO found
0 vehicles. Headlight mode learned 4 lanes from 75 headlight paths and counted 39
vehicles. Spot checks of the counted events show real vehicles at the counting line.
**Recall has not been measured yet:** a hand count with `hand_count.py` is the next step.

Limits:
- Every vehicle is recorded as `car`, because lights alone can't tell a car from a truck.
- Headlight glare on guardrails and wet roads can be detected as a moving light. It only
  counts if it crosses a counting line inside a lane, so check the lanes preview.
- Built for cameras in night mode on dark roads. On a well-lit road at night, the normal
  YOLO detector works (it counted vehicles on Caltrans D3-41, Hwy 51 at E St, under
  street lights), so use `--detector yolo` there.

## Camera view check

Lanes are learned once and then reused, which only works while the camera points the same
way. Caltrans operators pan and zoom, so every new recording is checked against the view
the lanes were learned from before any counting starts.

**Both sides are backgrounds, not single frames.** `auto_lanes.py` saves the per-pixel
**median of about 15 frames spread over 5 s** next to the lanes file as `<lanes>.reference.jpg`
(grayscale, at most 640 px wide, about 25-50 KB) and records its name as `"reference_image"`;
`check_video()` builds the same median for each sample it compares. Moving vehicles,
headlights and glare drop out and the static scene stays. This is not a refinement: in a
dark scene the only matchable detail in a single frame is headlights, and RANSAC will fit
their motion and call a still camera `moved`.

`track_count.py --view-check {off,warn,strict}` samples a background at **2 s, then 10 s
and 20 s** into the video (`--view-check-seconds` moves the first one) and prints one line:

```
camera view check: moved (shifted 6.1% (limit 3%); the same move in all 2 samples);
reference lanes_vc.reference.jpg; shift +38.5,+7.2 px (6.1% of the frame); scale 0.997;
rotation +0.06 deg; 1272/1349 inliers
```

| verdict | `warn` (default) | `strict` |
|---|---|---|
| `same` | counts | counts |
| `moved` | counts, with a warning | **stops** with `camera view changed: re-learn the lanes (…)` and no events are written |
| `unknown` | counts, with a note | counts, with a note |

`unknown` never stops, because the usual reason is day against night: the reference is
daytime, the check runs at night, and matching a lit road against a black one fails. That
is honest — a wrong `same` or `moved` would be worse than none.

Three rules keep `moved` honest:

- **It has to be consistent.** `moved` is reported only when at least two samples agree on
  the shift, the scale and the rotation (within 1% of the frame, 2% and 1°); disagreement
  gives `unknown`. A clip too short for a second sample can therefore never say `moved`.
- **It has to be plausible.** A measured scale outside `--view-plausible-scale 0.67 1.5` or
  a rotation past `--view-plausible-rotation 15` is `unknown`, because a 2× zoom or a 20°
  twist is far more likely mismatched features than a real move. On a dark still camera the
  old check produced exactly that: "shifted 12.3%, zoomed 2.015" with 44 of 57 matches.
- **It has to survive the overlays.** See below.

Method (`pipeline/view_check.py`): CLAHE, ORB features, a 0.75 ratio test, a RANSAC
homography, then the transform read as a translation, a scale and a rotation relative to
the frame size. Thresholds: `--view-max-shift 0.03` (3% of the longer side),
`--view-max-scale 0.05`, `--view-max-rotation 2` degrees.

**Overlays never move, so they are masked out.** The camera name burned into the top of a
Caltrans frame and the timestamp at the bottom stay put when the camera pans, and a naive
matcher would call that `same`. The top 25% and bottom 10% are ignored by default
(`--view-mask-top`, `--view-mask-bottom`), and a camera with a different layout can
override them with `"view_mask": {"top": 0.3, "bottom": 0.06}` in its lanes file.

A night reference can be added later with `"reference_image_night"`: when both are named,
the check compares against whichever is closer to the frame's mean brightness, which is
what lets "learn lanes by day, count by night" survive a camera that has moved.

The worker runs `track_count.py` with `--view-check strict`, so a moved camera fails the
job with `camera view changed: re-learn the lanes` instead of importing counts for lanes
that no longer describe the road.

## Known limits

- Accuracy drops at night, in rain, and with heavy occlusion.
- A vehicle whose box briefly merges with the one in front can be logged early.
- Accuracy is only spot-checked so far (see [Model settings](#model-settings)). Use [Measuring accuracy](#measuring-accuracy) to hand-count a full clip.
- Lanes assume a fixed camera. If the camera moves, redo the lanes file — the view check
  (below) is what notices.

## Snapshot density (FL511)

FL511 publishes a still image every minute or two, so there is no motion to track.
[`snapshot_density.py`](snapshot_density.py) measures **density** (vehicles per lane at
that moment) instead of flow: most cars pass between two snapshots, so flow counts are
not available from images. Capture with `python -m ingestion capture --camera-id <id> --once`,
draw a lanes file by hand from a snapshot (there are no vehicle paths to learn from), then:

```bash
python pipeline/snapshot_density.py data/live_footage/camera_1267/*.jpg \
    --lanes pipeline/lanes_fl511_1267.json --camera-id 1267 \
    --output data/output/fl511_1267_density.csv
```

One row per image per lane with per-class counts; rows are raw and unsmoothed, so
smoothing belongs in the query. Detection runs on the whole image plus overlapping tiles
(640 px with 20% overlap by default) and merges duplicates with class-aware NMS, because
distant vehicles in a snapshot camera are only a few pixels wide. Tiles are never smaller
than `--imgsz`: a smaller tile would be upscaled before detection, which is what made a
1280 run over-count. `--no-tiles` measures the single-pass behaviour for comparison, and
`--preview` writes a JPEG of the lanes and boxes for checking a hand-drawn lanes file.

After NMS a class-agnostic pass merges any two boxes that overlap by `--merge-ios` of the
smaller one (default 0.6). It keeps the larger geometry, because the lane is assigned from
a box's bottom-centre and a tile-edge fragment's bottom-centre can fall in the wrong lane,
and takes the class from the higher-scoring box. That removes both the fragments and the
car/truck pairs NMS keeps on one vehicle.

**Defaults: `yolo26m.pt`, `--imgsz 640`, tiled, `--conf 0.2`.** Tiled detection at 640 was
measured against a hand count on `vehicles-2.mp4`, three frames counted lane by lane
(0000, 0240, 0480), with `pipeline/lanes_vehicles-2.json`:

| configuration | mean absolute error, 6 lanes | bias | frame 0240, lanes 1–6 |
|---------------|------------------------------|------|-----------------------|
| **tiled, 640 (default)** | **4.7 vehicles/frame** | −1.3 | 3, 5, 5, 5, 8, 8 |
| plain, 1280 | 8.0 | −4.7 | 3, 4, 4, 5, 6, 6 |
| tiled, 1280 | 13.7 | +12.3 | 3, 5, 6, 8, 11, 12 |
| plain, 640 | 17.0 | −15.7 | 3, 2, 2, 4, 4, 3 |

Hand counts for those frames: 31, 35 and 34 vehicles in total. The same frame-0240
per-lane result was reproduced on a GPU with the same settings, and a second reader
counted about 37 for that frame against 35, which is the size of the counting uncertainty.

At 640 the single pass really does miss roughly half the vehicles and the tiles recover
them; at 1280 the single pass already sees most of them, so tiles mostly add duplicates.
That is why the default is tiled 640 and not the larger model input size.

FL511 refreshes a camera's still about every **three minutes**, so a capture loop polls every
**180 s**; polling every 90 s returns the same frame on every second poll.

FDOT confirmed on 2026-10-29 that the project may use FL511 with no end date, public display
included; `EXPIRY_DATE` in `snapshot_density.py` is `None`, and the check there still refuses
after a date if one is ever set again. Anything that captures on this tool's behalf inherits
the same rule.
