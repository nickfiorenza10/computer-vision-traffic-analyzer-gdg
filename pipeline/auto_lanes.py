"""Generate lanes.json automatically by learning lanes from vehicle paths.

Tracks vehicles for the first part of a video, keeps the ones that drove somewhere in a
fairly straight line, groups them by heading into flows (one per direction of traffic),
and turns each flow into one lane polygon, following the road's perspective, with a
counting line across it. So a two-way road gets two lanes, inbound and outbound; the
individual lanes within a direction are not separated (that proved unreliable from a
minute of video), and a person can draw them instead.

The file records a quality verdict: lanes are usable without a person checking them only
when no flows cross (an intersection or ramp), every lane was seen by enough vehicles, no
two lanes overlap, and the lanes explain most of the traffic. Otherwise check the preview.

Usage:
    python pipeline/auto_lanes.py data/sample.mp4
    python pipeline/auto_lanes.py data/sample.mp4 --seconds 120 --output pipeline/lanes_cam2.json
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import supervision as sv
from ultralytics import YOLO

import view_check
from headlights import HeadlightDetector, make_tracker, track
from model_source import resolve_model_cli

# COCO class IDs for the vehicle classes in the events.csv schema
VEHICLE_CLASSES = [1, 2, 3, 5, 7]

# A path must cover this share of the frame diagonal (or 1.5 vehicle sizes) to count,
# and be at least this straight (displacement / distance travelled).
MIN_TRAVEL = 0.08
STRAIGHTNESS = 0.6
# Paths within this many degrees of each other's heading are the same flow of traffic;
# flows this far apart cross each other (an intersection), rather than run two ways.
FLOW_TOLERANCE = 35
CROSSING = (40, 140)
# A direction's lane runs from its outermost vehicles' centres (5th and 95th percentile,
# in lane units: a vehicle box's size across the flow) plus half a vehicle either side.
EDGE_PADDING = 0.5
# Lanes are used without a person checking them only when every lane was seen by this
# many vehicles, no two lanes overlap by more than this share, and the lanes explain at
# least this share of the paths.
APPROVE_MIN_VEHICLES = 3
MAX_OVERLAP = 0.15
MIN_EXPLAINED = 0.6
# Overlapping lanes lose their padding first, then are shortened from their far end in
# steps of this share of their length, at most this much in all.
TRIM_STEP = 0.05
MAX_TRIM = 0.3


def parse_args():
    parser = argparse.ArgumentParser(description="Learn lane polygons from vehicle paths.")
    parser.add_argument("source", type=Path, help="Path to the input video")
    parser.add_argument("--output", type=Path, default=Path("pipeline/lanes.json"), help="Where to write lanes.json")
    parser.add_argument("--seconds", type=float, default=60, help="How much video to learn from (default: 60)")
    parser.add_argument("--min-tracks", type=int, default=2, help="Vehicles needed to accept a lane (default: 2)")
    parser.add_argument("--model", default="yolo26m.pt", help="YOLO weights (default: yolo26m.pt)")
    parser.add_argument(
        "--detector",
        choices=["yolo", "headlights"],
        default="yolo",
        help="yolo (default), or headlights for dark night video (pipeline/headlights.py)",
    )
    parser.add_argument("--conf", type=float, default=0.2, help="Minimum confidence (default: 0.2)")
    parser.add_argument(
        "--imgsz",
        type=int,
        default=1280,
        help="Size YOLO resizes frames to; larger finds small, distant vehicles (default: 1280)",
    )
    parser.add_argument("--device", default=None, help="'cpu', '0' for the first GPU, etc.")
    parser.add_argument(
        "--tracker",
        default="bytetrack.yaml",
        help="bytetrack.yaml or botsort.yaml (default: bytetrack.yaml)",
    )
    return parser.parse_args()


def collect_tracks(source, model_path, seconds, conf, device, tracker, imgsz):
    """Track vehicles and return each one's path of (x, y, box width, box height) points."""
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    max_frames = int(seconds * fps)
    tracks = defaultdict(list)
    first_frame = None
    results = YOLO(resolve_model_cli(model_path, "auto_lanes.py")).track(
        source=str(source),
        classes=VEHICLE_CLASSES,
        conf=conf,
        imgsz=imgsz,
        device=device,
        tracker=tracker,
        persist=True,
        stream=True,
        verbose=False,
    )
    for frame_idx, result in enumerate(results):
        if first_frame is None:
            first_frame = result.orig_img.copy()
        if frame_idx >= max_frames:
            break
        if result.boxes.id is None:
            continue
        for track_id, (x1, y1, x2, y2) in zip(result.boxes.id.int().tolist(), result.boxes.xyxy.tolist()):
            # Bottom-center is where the vehicle touches the road
            tracks[track_id].append(((x1 + x2) / 2, y2, x2 - x1, y2 - y1))

    return {k: np.array(v) for k, v in tracks.items()}, width, height, first_frame


def collect_headlight_tracks(source, seconds, tracker):
    """Like collect_tracks, but from headlight blobs (pipeline/headlights.py) for dark video."""
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    max_frames = int(seconds * fps)
    detector = HeadlightDetector()
    bytetrack = make_tracker(tracker)
    tracks = defaultdict(list)
    first_frame = None
    for _ in range(max_frames):
        ok, frame = cap.read()
        if not ok:
            break
        if first_frame is None:
            first_frame = frame.copy()
        for x1, y1, x2, y2, track_id, _, _ in track(bytetrack, detector.detect(frame), frame.shape):
            tracks[int(track_id)].append(((x1 + x2) / 2, y2, x2 - x1, y2 - y1))
    cap.release()
    return {k: np.array(v) for k, v in tracks.items()}, width, height, first_frame


def moving_paths(tracks, width, height):
    """Paths that clearly went somewhere, in a fairly straight line.

    Parked cars and flickering boxes would make fake lanes, and a turning vehicle's path
    belongs to no single lane, so both are left out.
    """
    diagonal = float(np.hypot(width, height))
    paths = []
    for path in tracks.values():
        if len(path) < 10:
            continue
        travelled = float(np.hypot(*np.diff(path[:, :2], axis=0).T).sum())
        displacement = float(np.hypot(*(path[-1, :2] - path[0, :2])))
        size = float(np.median(np.maximum(path[:, 2], path[:, 3])))
        if displacement < max(MIN_TRAVEL * diagonal, 1.5 * size) or travelled == 0:
            continue
        if displacement / travelled < STRAIGHTNESS:
            continue
        paths.append(path)
    return paths


def heading_of(path):
    d = path[-1, :2] - path[0, :2]
    return float(np.arctan2(d[1], d[0]))


def angle_between(a, b):
    """The smaller angle between two headings, in degrees (0..180)."""
    return abs((np.degrees(a - b) + 180.0) % 360.0 - 180.0)


def group_flows(paths):
    """Paths grouped by heading: each group is one direction of traffic (a flow)."""
    headings = [heading_of(p) for p in paths]
    remaining = list(range(len(paths)))
    flows = []
    while remaining:
        # Seed each flow at the heading with the most company, so a lone odd path never
        # pulls a real flow toward itself.
        company = [sum(angle_between(headings[i], headings[j]) <= FLOW_TOLERANCE for j in remaining) for i in remaining]
        seed = remaining[int(np.argmax(company))]
        members = [i for i in remaining if angle_between(headings[i], headings[seed]) <= FLOW_TOLERANCE]
        flows.append(members)
        remaining = [i for i in remaining if i not in members]
    return flows


def heading_name(u):
    """Moving down the frame is toward the camera: "inbound"; mostly sideways is left/right."""
    if abs(u[1]) >= 0.5 * abs(u[0]):
        return "inbound" if u[1] > 0 else "outbound"
    return "rightward" if u[0] > 0 else "leftward"


def travel_counts_as_in(line, u):
    """Does a vehicle travelling along `u` cross `line` as supervision's "in"?"""
    zone = sv.LineZone(sv.Point(*line[0]), sv.Point(*line[1]), triggering_anchors=(sv.Position.CENTER,))
    middle = (np.asarray(line[0], float) + np.asarray(line[1], float)) / 2
    crossed_in = False
    for offset in (-8.0, 8.0):
        x, y = middle + offset * np.asarray(u, float)
        detections = sv.Detections(xyxy=np.array([[x - 1, y - 1, x + 1, y + 1]]), tracker_id=np.array([1]))
        crossed_in, _ = zone.trigger(detections)
    return bool(np.any(crossed_in))


def flow_lanes(paths, width, height, min_tracks):
    """The lane for one direction of traffic. Returns ([lane] or [], paths explained, heading)."""
    displacement = np.array([p[-1, :2] - p[0, :2] for p in paths])
    u = (displacement / np.linalg.norm(displacement, axis=1, keepdims=True)).mean(axis=0)
    u /= np.linalg.norm(u)
    n = np.array([-u[1], u[0]])

    def along(points):
        return points[:, :2] @ u

    def across(points):
        return points[:, :2] @ n

    # How wide a vehicle looks across the flow, as a straight-line function of how far
    # along the road it is: it shrinks toward the horizon, and lanes shrink with it.
    points = np.vstack(paths)
    a, c = along(points), across(points)
    extent = np.abs(points[:, 2] * n[0]) + np.abs(points[:, 3] * n[1])
    bands = np.linspace(a.min(), a.max(), 11)
    centres, sizes = [], []
    for lo, hi in zip(bands[:-1], bands[1:]):
        inside = (a >= lo) & (a <= hi)
        if inside.sum() >= 3:
            centres.append((lo + hi) / 2)
            sizes.append(np.median(extent[inside]))
    size_fit = np.polyfit(centres, sizes, 1) if len(centres) >= 2 else np.array([0.0, np.median(extent)])
    floor = max(2.0, 0.25 * float(np.median(extent)))

    def size(v):
        return np.maximum(np.polyval(size_fit, v), floor)

    # The road's own axis: a straight line through all the traffic. Converging lanes all
    # head for the same vanishing point, so a vehicle's offset from this axis, divided by
    # the vehicle size at that depth, stays about constant along its path: in these "lane
    # units" the band the traffic drives in has straight edges.
    axis = np.polyfit(a, c, 1)

    def lane_units(points):
        v = along(points)
        return (across(points) - np.polyval(axis, v)) / size(v)

    # One lane per direction: the band its vehicles drove in. Splitting a direction into its
    # individual lanes from a minute of video proved unreliable on real cameras (a box's
    # size is only roughly a vehicle's width when it is seen at an angle), so that is left
    # to a person drawing them.
    offsets = np.array([float(np.median(lane_units(p))) for p in paths])
    inner_left, inner_right = float(np.percentile(offsets, 5)), float(np.percentile(offsets, 95))
    cars = [p for p, o in zip(paths, offsets) if inner_left - EDGE_PADDING <= o <= inner_right + EDGE_PADDING]
    if len(cars) < min_tracks:
        return [], 0, u

    def to_image(v, units):
        cross = np.polyval(axis, v) + units * size(v)
        x, y = v * u + cross * n
        return float(np.clip(x, 0, width - 1)), float(np.clip(y, 0, height - 1))

    travel = np.concatenate([along(p) for p in cars])
    lo, hi = np.percentile(travel, 5), np.percentile(travel, 95)
    far_is_lo = size(lo) < size(hi)
    travel_name, opposite_name = heading_name(u), heading_name(-u)

    def shape(trim=0.0, padding=EDGE_PADDING):
        """The lane's polygon, counting line and directions: `padding` (lane units) beyond
        its outermost vehicles either side, and `trim` of its length cut off the far end
        (toward the horizon), where neighbouring lanes converge."""
        left, right = inner_left - padding, inner_right + padding
        start, end = (lo + trim * (hi - lo), hi) if far_is_lo else (lo, hi - trim * (hi - lo))
        # The counting line goes where most of the vehicles pass, nearer the camera (bigger
        # vehicles) when there is a tie.
        candidates = np.linspace(start + 0.15 * (end - start), end - 0.15 * (end - start), 15)
        coverage = np.array([sum(along(p).min() <= v <= along(p).max() for p in cars) for v in candidates])
        best = np.flatnonzero(coverage == coverage.max())
        v_line = candidates[max(best, key=lambda i: size(candidates[i]))]
        polygon = [to_image(start, left), to_image(start, right), to_image(end, right), to_image(end, left)]
        line = [to_image(v_line, left), to_image(v_line, right)]
        directions = (
            {"in": travel_name, "out": opposite_name}
            if travel_counts_as_in(line, u)
            else {"in": opposite_name, "out": travel_name}
        )
        return {
            "polygon": [[int(round(x)), int(round(y))] for x, y in polygon],
            "line": [[int(round(x)), int(round(y))] for x, y in line],
            "directions": directions,
        }

    lane = {
        "shape": shape,
        "vehicles_seen": len(cars),
        "order": float(np.polyval(axis, (lo + hi) / 2)),
    }
    return [lane], len(cars), u


def overlap_share(first, second, width, height):
    """How much of the smaller of two polygons the other one covers (0..1)."""
    masks = []
    for polygon in (first, second):
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [np.asarray(polygon, dtype=np.int32).reshape(-1, 1, 2)], 1)
        masks.append(mask.astype(bool))
    smaller = min(masks[0].sum(), masks[1].sum())
    return float((masks[0] & masks[1]).sum() / smaller) if smaller else 0.0


def build_lanes(tracks, width, height, min_tracks):
    """Cluster vehicle paths into lanes. Returns (lanes, info) in pixel coordinates.

    `info["quality"]` says whether the lanes can be used without a person checking them,
    and if not, why: crossing flows (an intersection), lanes seen by too few vehicles,
    lanes that overlap, or too little of the traffic explained by the lanes.
    """
    paths = moving_paths(tracks, width, height)
    if not paths:
        raise SystemExit("No moving vehicles found. Try a longer --seconds or a different clip.")

    flows = [f for f in group_flows(paths) if len(f) >= min_tracks]
    problems = []
    headings = []
    lanes = []
    explained = 0
    for flow in flows:
        flow_paths = [paths[i] for i in flow]
        found, covered, u = flow_lanes(flow_paths, width, height, min_tracks)
        explained += covered
        if found:
            headings.append((float(np.arctan2(u[1], u[0])), len(flow)))
        lanes.extend((len(headings), lane) for lane in found)
    if not lanes:
        raise SystemExit("No lane had enough vehicles. Try a longer --seconds or a lower --min-tracks.")

    # Two-way traffic is ~180 degrees apart; flows meeting at an angle cross each other.
    for i in range(len(headings)):
        for j in range(i + 1, len(headings)):
            angle = angle_between(headings[i][0], headings[j][0])
            if CROSSING[0] <= angle <= CROSSING[1] and min(headings[i][1], headings[j][1]) >= APPROVE_MIN_VEHICLES:
                problems.append(
                    f"traffic crosses at {angle:.0f} degrees (an intersection or a ramp); check the lanes by hand"
                )
                break

    # Lanes left to right within each flow, flows in the order they were found.
    lanes.sort(key=lambda item: (item[0], item[1]["order"]))
    built = [lane for _flow, lane in lanes]

    # Lanes of neighbouring directions can overlap two ways. Side by side, when the padding
    # beyond the outermost vehicles reaches over the median: narrow the padding first. At
    # the far end, where two directions of one road converge toward the horizon and the
    # vehicles are a few pixels: then pull the far ends back, up to MAX_TRIM of the length.
    # What still overlaps after both is real, and is reported.
    steps = [(0.0, EDGE_PADDING * (1 - k / 4)) for k in range(5)]
    steps += [(TRIM_STEP * k, 0.0) for k in range(1, int(MAX_TRIM / TRIM_STEP) + 1)]
    stage = [0] * len(built)
    shapes = [lane["shape"](*steps[0]) for lane in built]
    while True:
        bumped = set()
        for i in range(len(built)):
            for j in range(i + 1, len(built)):
                if overlap_share(shapes[i]["polygon"], shapes[j]["polygon"], width, height) > MAX_OVERLAP:
                    bumped.update(k for k in (i, j) if stage[k] < len(steps) - 1)
        if not bumped:
            break
        for k in bumped:
            stage[k] += 1
            shapes[k] = built[k]["shape"](*steps[stage[k]])

    output = []
    for number, (lane, lane_shape) in enumerate(zip(built, shapes), start=1):
        output.append({"id": number, **lane_shape, "vehicles_seen": lane["vehicles_seen"]})

    for lane in output:
        if lane["vehicles_seen"] < APPROVE_MIN_VEHICLES:
            problems.append(f"lane {lane['id']} was seen by only {lane['vehicles_seen']} vehicles")
    for i in range(len(output)):
        for j in range(i + 1, len(output)):
            share = overlap_share(output[i]["polygon"], output[j]["polygon"], width, height)
            if share > MAX_OVERLAP:
                problems.append(f"lanes {output[i]['id']} and {output[j]['id']} overlap ({share:.0%})")
    if explained < MIN_EXPLAINED * len(paths):
        problems.append(f"the lanes explain only {explained} of {len(paths)} vehicle paths")

    info = {
        "paths": len(paths),
        "flows": len(headings),
        "dropped_lanes": 0,
        "quality": {"approved": not problems, "problems": problems},
    }
    return output, info


def draw_preview(frame, lanes):
    for lane in lanes:
        cv2.polylines(frame, [np.array(lane["polygon"]).reshape(-1, 1, 2)], True, (255, 200, 0), 4)
        (x1, y1), (x2, y2) = lane["line"]
        cv2.line(frame, (x1, y1), (x2, y2), (0, 0, 255), 5)
        cv2.putText(frame, str(lane["id"]), (x1 + 10, y1 - 15), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 255), 4)
    return frame


def generate_lanes(
    source,
    output,
    seconds=60,
    min_tracks=2,
    model="yolo26m.pt",
    conf=0.2,
    device=None,
    tracker="bytetrack.yaml",
    imgsz=1280,
    detector="yolo",
):
    """Learn lanes from a video, write lanes.json and a preview image. Returns the preview path."""
    print(f"Learning lanes from the first {seconds:g}s of {source} ({detector})...")
    if detector == "headlights":
        tracks, width, height, first_frame = collect_headlight_tracks(source, seconds, tracker)
    else:
        tracks, width, height, first_frame = collect_tracks(source, model, seconds, conf, device, tracker, imgsz)
    lanes, info = build_lanes(tracks, width, height, min_tracks)

    # The reference image is what a later recording is compared against: if the camera
    # has been panned or zoomed since, the lanes no longer describe the road. It is the
    # median of a few seconds of footage, so passing vehicles and their lights are not
    # part of what the camera is remembered as.
    output.parent.mkdir(parents=True, exist_ok=True)
    background = view_check.background_from_video(source)
    if background is None:
        background = first_frame
    reference = view_check.save_reference(background, output)

    config = {
        "camera": source.stem,
        "note": f"Generated by auto_lanes.py from {info['paths']} vehicle paths. Check the preview image.",
        "method": "paths-v2",
        "frame_size": [width, height],
        "reference_image": reference,
        "quality": info["quality"],
        "lanes": lanes,
    }
    output.write_text(json.dumps(config, indent=2))

    preview = Path("data/output") / f"{source.stem}_lanes.jpg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(preview), draw_preview(first_frame, lanes))

    print(f"Found {len(lanes)} lanes in {info['flows']} flows from {info['paths']} vehicle paths -> {output}")
    for lane in lanes:
        print(f"  lane {lane['id']}: {lane['vehicles_seen']} vehicles, {lane['directions']}")
    quality = info["quality"]
    if quality["approved"]:
        print("Quality: passes every check; usable without a person checking the preview.")
    else:
        print("Quality: needs a person to check the preview:")
        for problem in quality["problems"]:
            print(f"  - {problem}")
    print(f"Preview image: {preview}")
    print(f"Reference image for the view check: {output.with_name(reference)}")
    return preview


def main():
    args = parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Video not found: {args.source}")
    generate_lanes(
        args.source,
        args.output,
        args.seconds,
        args.min_tracks,
        args.model,
        args.conf,
        args.device,
        args.tracker,
        args.imgsz,
        args.detector,
    )


if __name__ == "__main__":
    main()
