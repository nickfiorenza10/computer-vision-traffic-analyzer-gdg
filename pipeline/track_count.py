"""Track vehicles in a video, count lane-line crossings, and write events.csv.

Each lane in lanes.json has a polygon (the lane area) and a counting line.
A vehicle is logged once, the first time its bottom-center point crosses a
lane's line while inside that lane's polygon. Columns match the events.csv
schema in the README.

Usage:
    python pipeline/track_count.py data/sample.mp4 --start 2026-10-09T08:00:00
    python pipeline/track_count.py data/sample.mp4 --lanes pipeline/lanes.json --save-video
    python pipeline/track_count.py data/sample.mp4 --auto-lanes --lanes pipeline/lanes_cam2.json
    python pipeline/track_count.py data/sample.mp4 --results-json   # also write results for the dashboard
    python pipeline/track_count.py data/night.ts --detector headlights --auto-lanes --lanes pipeline/lanes_cam3.json
"""

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import supervision as sv
from ultralytics import YOLO

import view_check
from auto_lanes import generate_lanes
from headlights import HeadlightDetector, make_tracker, track
from lane_assign import MajorityLanes
from model_source import resolve_model_cli

# COCO class IDs for the vehicle classes in the events.csv schema
VEHICLE_CLASSES = {1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
CLASS_NAMES = list(VEHICLE_CLASSES.values())  # index order used in the dashboard results file
EVENT_COLUMNS = ["timestamp", "frame", "track_id", "class", "lane", "direction"]
ANCHOR = sv.Position.BOTTOM_CENTER


def parse_args():
    parser = argparse.ArgumentParser(description="Track and count vehicles per lane.")
    parser.add_argument("source", type=Path, help="Path to the input video")
    parser.add_argument("--lanes", type=Path, default=Path("pipeline/lanes.json"), help="Lane definitions")
    parser.add_argument(
        "--auto-lanes",
        action="store_true",
        help="Learn lanes from this video first (see auto_lanes.py) and save them to --lanes",
    )
    parser.add_argument(
        "--auto-seconds",
        type=float,
        default=60,
        help="With --auto-lanes, how much video to learn lanes from (default: 60)",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Real-world clip start time, e.g. 2026-10-09T08:00:00 (default: video file's modified time)",
    )
    parser.add_argument("--model", default="yolo26m.pt", help="YOLO weights (default: yolo26m.pt)")
    parser.add_argument(
        "--detector",
        choices=["yolo", "headlights"],
        default="yolo",
        help="yolo (default), or headlights for dark night video where only lights are visible "
        "(see pipeline/README.md, 'Night: headlight mode')",
    )
    parser.add_argument(
        "--tracker",
        default="bytetrack.yaml",
        help="bytetrack.yaml or botsort.yaml (default: bytetrack.yaml)",
    )
    parser.add_argument("--conf", type=float, default=0.2, help="Minimum confidence (default: 0.2)")
    parser.add_argument(
        "--imgsz",
        type=int,
        default=1280,
        help="Size YOLO resizes frames to; larger finds small, distant vehicles (default: 1280)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="'cpu', '0' for the first GPU, etc. Default: GPU if available, else CPU",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="events CSV path (default: data/output/<name>_events.csv)",
    )
    parser.add_argument("--save-video", action="store_true", help="Also save an annotated video")
    parser.add_argument(
        "--results-json",
        action="store_true",
        help="Also write <name>_results.json (boxes, track IDs, lanes and events) for the web dashboard",
    )
    parser.add_argument(
        "--results-every",
        type=int,
        default=1,
        help="With --results-json, keep boxes for every Nth frame to shrink the file (default: 1, every frame)",
    )
    parser.add_argument("--show", action="store_true", help="Show a live preview window (press q to stop)")
    parser.add_argument(
        "--view-check",
        choices=("off", "warn", "strict"),
        default="warn",
        help=(
            "Compare this video's view with the reference image saved beside the lanes "
            "file: off skips it, warn prints the result and carries on (default), strict "
            "stops on 'moved'. 'unknown' never stops, because day-against-night usually "
            "cannot be measured."
        ),
    )
    parser.add_argument(
        "--view-check-seconds",
        type=float,
        default=2.0,
        help=(
            "Where the first view-check sample starts (default: 2 s). Further samples are "
            "taken 8 s and 18 s later when the video is long enough; 'moved' is only "
            "reported when they agree."
        ),
    )
    parser.add_argument(
        "--view-plausible-scale",
        type=float,
        nargs=2,
        metavar=("MIN", "MAX"),
        default=[view_check.DEFAULT_MIN_PLAUSIBLE_SCALE, view_check.DEFAULT_MAX_PLAUSIBLE_SCALE],
        help=(
            "A measured zoom outside this range is reported as unknown, not moved, because "
            "it is far likelier mismatched features than a real zoom "
            f"(default: {view_check.DEFAULT_MIN_PLAUSIBLE_SCALE:g} {view_check.DEFAULT_MAX_PLAUSIBLE_SCALE:g})"
        ),
    )
    parser.add_argument(
        "--view-plausible-rotation",
        type=float,
        default=view_check.DEFAULT_MAX_PLAUSIBLE_ROTATION,
        help=(
            "A measured rotation above this many degrees is reported as unknown, not moved "
            f"(default: {view_check.DEFAULT_MAX_PLAUSIBLE_ROTATION:g})"
        ),
    )
    parser.add_argument(
        "--view-max-shift",
        type=float,
        default=view_check.DEFAULT_MAX_SHIFT,
        help=(
            "How far the view may drift before it counts as moved, as a fraction of the "
            f"frame's longer side (default: {view_check.DEFAULT_MAX_SHIFT:g} = "
            f"{view_check.DEFAULT_MAX_SHIFT:.0%})"
        ),
    )
    parser.add_argument(
        "--view-max-scale",
        type=float,
        default=view_check.DEFAULT_MAX_SCALE,
        help=f"How much zoom counts as moved, relative (default: {view_check.DEFAULT_MAX_SCALE:g} = 5%)",
    )
    parser.add_argument(
        "--view-max-rotation",
        type=float,
        default=view_check.DEFAULT_MAX_ROTATION,
        help=f"How much rotation counts as moved, in degrees (default: {view_check.DEFAULT_MAX_ROTATION:g})",
    )
    parser.add_argument(
        "--view-mask-top",
        type=float,
        default=None,
        help=(
            "Fraction of the top of the frame to ignore, where the camera name is burned "
            f"in (default: {view_check.DEFAULT_MASK_TOP:g}, or the lanes file's view_mask.top)"
        ),
    )
    parser.add_argument(
        "--view-mask-bottom",
        type=float,
        default=None,
        help=(
            "Fraction of the bottom to ignore, where the timestamp is burned in "
            f"(default: {view_check.DEFAULT_MASK_BOTTOM:g}, or the lanes file's view_mask.bottom)"
        ),
    )
    return parser.parse_args()


def load_lanes(path, width, height):
    """Read lanes.json and build a polygon zone and counting line for each lane.

    Coordinates are scaled from the file's frame_size to the video's size, so
    the same lanes work for a 4K original and a 1080p copy of the same camera.
    """
    config = json.loads(path.read_text())
    lane_w, lane_h = config.get("frame_size", (width, height))
    sx, sy = width / lane_w, height / lane_h
    if abs(sx - sy) > 0.01:
        print(
            f"WARNING: {path} was drawn for {lane_w}x{lane_h} but the video is {width}x{height} "
            "(different aspect ratio). Lanes may be in the wrong place."
        )

    def scale(points):
        return np.array([[round(x * sx), round(y * sy)] for x, y in points], dtype=np.int64)

    lanes = []
    for lane in config["lanes"]:
        polygon = scale(lane["polygon"])
        (x1, y1), (x2, y2) = scale(lane["line"]).tolist()
        lanes.append(
            {
                "id": lane["id"],
                "polygon": polygon,
                "zone": sv.PolygonZone(polygon, triggering_anchors=(ANCHOR,)),
                "line": sv.LineZone(sv.Point(x1, y1), sv.Point(x2, y2), triggering_anchors=(ANCHOR,)),
                "line_points": ((x1, y1), (x2, y2)),
                "directions": lane.get("directions", {"in": "in", "out": "out"}),
            }
        )
    return lanes


def clip_start_time(args):
    if args.start:
        return datetime.fromisoformat(args.start)
    start = datetime.fromtimestamp(args.source.stat().st_mtime)
    print(f"No --start given; using the video file's modified time: {start.isoformat()}")
    return start


def draw_lanes(frame, lanes, counts):
    for lane in lanes:
        cv2.polylines(frame, [lane["polygon"].reshape(-1, 1, 2)], True, (255, 200, 0), 2)
        p1, p2 = lane["line_points"]
        cv2.line(frame, p1, p2, (0, 0, 255), 3)
        cv2.putText(
            frame,
            f"lane {lane['id']}: {counts[lane['id']]}",
            (p1[0], p1[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
        )
    return frame


def frame_boxes(detections):
    """One frame's tracked boxes as compact rows: [track_id, class_index, confidence %, x1, y1, x2, y2]."""
    if detections.tracker_id is None:
        return []
    confidences = detections.confidence if detections.confidence is not None else [None] * len(detections)
    rows = []
    for box, track_id, class_id, conf in zip(detections.xyxy, detections.tracker_id, detections.class_id, confidences):
        x1, y1, x2, y2 = (int(round(float(v))) for v in box)
        rows.append([
            int(track_id),
            CLASS_NAMES.index(VEHICLE_CLASSES[int(class_id)]),
            None if conf is None else int(round(float(conf) * 100)),
            x1, y1, x2, y2,
        ])
    return rows


def yolo_frames(args):
    """Tracked detections per frame from YOLO + the configured tracker."""
    model = YOLO(resolve_model_cli(args.model, "track_count.py"))
    results = model.track(
        source=str(args.source),
        classes=list(VEHICLE_CLASSES),
        conf=args.conf,
        imgsz=args.imgsz,
        device=args.device,
        tracker=args.tracker,
        persist=True,
        stream=True,
        verbose=False,
    )
    for result in results:
        yield sv.Detections.from_ultralytics(result), result.plot
    yield None, str(model.predictor.device)


def headlight_frames(args):
    """Tracked detections per frame from headlight blobs (pipeline/headlights.py)."""
    detector = HeadlightDetector()
    tracker = make_tracker(args.tracker)
    cap = cv2.VideoCapture(str(args.source))
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        tracked = track(tracker, detector.detect(frame), frame.shape)
        if len(tracked):
            detections = sv.Detections(
                xyxy=tracked[:, :4].astype(np.float32),
                confidence=tracked[:, 5].astype(np.float32),
                class_id=tracked[:, 6].astype(int),
                tracker_id=tracked[:, 4].astype(int),
            )
        else:
            detections = sv.Detections.empty()

        def plot(frame=frame, detections=detections):
            image = frame.copy()
            if detections.tracker_id is not None:
                for (x1, y1, x2, y2), track_id in zip(detections.xyxy.astype(int), detections.tracker_id):
                    cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(image, f"id:{track_id} lights", (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
            return image

        yield detections, plot
    cap.release()
    yield None, "cpu (headlights)"


def write_results_json(path, *, source, fps, width, height, frames, start_time, args, lanes_config, events, track_classes, boxes):
    """Everything the dashboard needs to replay this video: boxes per frame, lanes and counting events."""
    payload = {
        "format": "traffic-analyzer-results/1",
        "video": source.name,
        "fps": fps,
        "frame_size": [width, height],
        "frames_total": frames,
        "start": start_time.isoformat(),
        "model": args.model,
        "tracker": args.tracker,
        "conf": args.conf,
        "classes": CLASS_NAMES,
        "lanes": lanes_config,
        "events": events,
        "track_classes": {str(k): v.most_common(1)[0][0] for k, v in track_classes.items() if v},
        "boxes_every": args.results_every,
        "boxes": boxes,  # [[frame_index, [[track_id, class_index, conf %, x1, y1, x2, y2], ...]], ...]
    }
    path.write_text(json.dumps(payload, separators=(",", ":")))


def run_view_check(args, lanes_path: Path):
    """Check the video's view against the lanes file's reference image before counting.

    `warn` prints and carries on, `strict` stops on `moved`, and `unknown` never stops:
    a night frame against a daytime reference usually cannot be measured, and refusing
    to count every night would make the check useless for the thing it exists for.
    """
    if args.view_check == "off":
        return None
    verdict = view_check.check_video(
        lanes_path,
        args.source,
        at_seconds=args.view_check_seconds,
        max_shift=args.view_max_shift,
        max_scale=args.view_max_scale,
        max_rotation=args.view_max_rotation,
        mask_top=args.view_mask_top,
        mask_bottom=args.view_mask_bottom,
        min_plausible_scale=args.view_plausible_scale[0],
        max_plausible_scale=args.view_plausible_scale[1],
        max_plausible_rotation=args.view_plausible_rotation,
    )
    print(verdict.describe())
    if verdict.verdict == view_check.MOVED and args.view_check == "strict":
        raise SystemExit(
            f"camera view changed: re-learn the lanes ({verdict.reason}). {lanes_path} was "
            "learned from a different view, so counting did not start."
        )
    if verdict.verdict == view_check.MOVED:
        print(
            "Warning: the camera view has moved, so these lanes may not line up. "
            "Re-learn them with auto_lanes.py before trusting the counts."
        )
    elif verdict.verdict == view_check.UNKNOWN:
        print("Note: the camera view could not be checked; counting continues.")
    return verdict


def main():
    args = parse_args()
    if args.results_every < 1:
        raise SystemExit("--results-every must be 1 or more")
    if not args.source.is_file():
        raise SystemExit(f"Video not found: {args.source}")
    if args.auto_lanes:
        generate_lanes(
            args.source,
            args.lanes,
            args.auto_seconds,
            model=args.model,
            conf=args.conf,
            imgsz=args.imgsz,
            device=args.device,
            tracker=args.tracker,
            detector=args.detector,
        )
    elif not args.lanes.is_file():
        raise SystemExit(f"Lanes file not found: {args.lanes} (use --auto-lanes to generate one)")

    cap = cv2.VideoCapture(str(args.source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    lanes = load_lanes(args.lanes, width, height)
    # Headlight boxes are grown upward from the lights, so the lane holding most of the box
    # decides, and crossings count on each lane's line widened to the frame (lane_assign.py).
    night = MajorityLanes(lanes, width, height) if args.detector == "headlights" else None
    run_view_check(args, args.lanes)
    start_time = clip_start_time(args)
    output = args.output or Path("data/output") / f"{args.source.stem}_events.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    writer = None
    if args.save_video:
        video_path = output.with_name(f"{args.source.stem}_tracked.mp4")
        writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

    if args.detector == "headlights":
        args.model = "headlights"  # recorded in --results-json instead of a weights file
    lanes_config = json.loads(args.lanes.read_text()) if args.results_json else None
    result_boxes = []  # per-frame boxes for --results-json
    events = []
    counted_ids = set()  # each vehicle is logged once, even if it recrosses a line
    # YOLO's class can flicker between frames (e.g. truck -> bus), so each
    # vehicle gets the class it was seen as most often over its whole track
    track_classes = defaultdict(Counter)
    counts = {lane["id"]: 0 for lane in lanes}
    frame_idx = -1
    start = time.perf_counter()

    device_label = "?"
    frame_source = headlight_frames(args) if args.detector == "headlights" else yolo_frames(args)
    for detections, plot in frame_source:
        if detections is None:
            device_label = plot  # the last item reports the device used
            break
        frame_idx += 1
        if args.results_json and frame_idx % args.results_every == 0:
            rows = frame_boxes(detections)
            if rows:
                result_boxes.append([frame_idx, rows])
        if detections.tracker_id is not None:
            for track_id, class_id in zip(detections.tracker_id, detections.class_id):
                track_classes[int(track_id)][VEHICLE_CLASSES[int(class_id)]] += 1
            members = night.members(detections) if night else None
            for index, lane in enumerate(lanes):
                # Only vehicles inside this lane can trigger its line
                if night:
                    in_lane = detections[members[index]]
                    crossed_in, crossed_out = night.lines[index].trigger(in_lane)
                else:
                    in_lane = detections[lane["zone"].trigger(detections)]
                    crossed_in, crossed_out = lane["line"].trigger(in_lane)
                for i in np.flatnonzero(crossed_in | crossed_out):
                    track_id = int(in_lane.tracker_id[i])
                    if track_id in counted_ids:
                        continue
                    counted_ids.add(track_id)
                    counts[lane["id"]] += 1
                    events.append(
                        {
                            "timestamp": (start_time + timedelta(seconds=frame_idx / fps)).isoformat(
                                timespec="milliseconds"
                            ),
                            "frame": frame_idx,
                            "track_id": track_id,
                            "class": None,  # filled in from track_classes after the video ends
                            "lane": lane["id"],
                            "direction": lane["directions"]["in" if crossed_in[i] else "out"],
                        }
                    )

        if writer or args.show:
            annotated = draw_lanes(plot(), lanes, counts)
            if writer:
                writer.write(annotated)
            if args.show:
                cv2.imshow("track_count", annotated)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

        if (frame_idx + 1) % 100 == 0:
            print(f"  frame {frame_idx + 1}/{total_frames}, {len(events)} vehicles counted")

    if writer:
        writer.release()
    cv2.destroyAllWindows()
    elapsed = time.perf_counter() - start
    frames = frame_idx + 1

    for event in events:
        event["class"] = track_classes[event["track_id"]].most_common(1)[0][0]
    pd.DataFrame(events, columns=EVENT_COLUMNS).to_csv(output, index=False)

    print(f"\nProcessed {frames} frames in {elapsed:.1f}s ({frames / max(elapsed, 1e-9):.1f} FPS) on {device_label}")
    print(f"Wrote {len(events)} events to {output}")
    if writer:
        print(f"Saved annotated video to {video_path}")
    if args.results_json:
        results_path = output.with_name(f"{args.source.stem}_results.json")
        write_results_json(
            results_path,
            source=args.source,
            fps=fps,
            width=width,
            height=height,
            frames=frames,
            start_time=start_time,
            args=args,
            lanes_config=lanes_config,
            events=events,
            track_classes=track_classes,
            boxes=result_boxes,
        )
        print(f"Wrote dashboard results to {results_path} (load it with 'Load pipeline results' on the dashboard)")
    for lane in lanes:
        print(f"  lane {lane['id']}: {counts[lane['id']]}")


if __name__ == "__main__":
    main()
