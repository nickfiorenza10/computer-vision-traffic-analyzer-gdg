"""Vehicle density per lane from FL511 snapshot images.

FL511 publishes a still image per camera every minute or two, so there is no
motion to track. This tool measures density instead of flow: how many vehicles
are in each lane at the moment the image was taken.

    python pipeline/snapshot_density.py data/live_footage/fl511_1043/*.jpg \
        --lanes pipeline/lanes_fl511_1043.json --camera-id 1043 \
        --output data/output/fl511_1043_density.csv

One row per image per lane: `captured_at,camera_id,image,lane,vehicle_count,car,
truck,bus,motorcycle,bicycle`. Rows are raw, unsmoothed; smoothing belongs in the
query, not in the file.

Detection is tiled by default because snapshot cameras look down long approaches
where distant vehicles are only a few pixels wide. Each image is scanned whole and
then in overlapping tiles, the tile boxes are mapped back to image coordinates,
and duplicates are merged with class-aware NMS. `--no-tiles` measures the plain
single-pass behaviour for comparison.

A vehicle belongs to a lane when its bottom-centre point is inside that lane's
polygon, using the same `lanes.json` format and resolution scaling as
`pipeline/track_count.py`. Lanes files for FL511 cameras are drawn by hand from a
snapshot because there are no vehicle paths to learn from; check the preview image
before committing one.

Capture is not done here: `python -m ingestion capture --camera-id <id> --once`
writes the images, under FDOT's permission of 2026-10-29 (no end date), and this
tool only reads files from disk.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

# COCO ids for the classes in the events.csv schema; must match
# VEHICLE_CLASSES in pipeline/track_count.py.
VEHICLE_CLASSES = {1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
CLASS_ORDER = ["car", "truck", "bus", "motorcycle", "bicycle"]

DEFAULT_MODEL = "yolo26m.pt"  # the pipeline's tested setting
# Tiled detection at 640 measured best against a hand count on 3 frames of
# vehicles-2.mp4 (mean absolute error 4.7 vehicles/frame, against 8.0 for plain
# 1280, 13.7 for tiled 1280 and 17.0 for plain 640). See pipeline/README.md.
DEFAULT_IMGSZ = 640
DEFAULT_CONF = 0.2
DEFAULT_TILE = 640
DEFAULT_OVERLAP = 0.2
DEFAULT_IOU = 0.55
DEFAULT_MERGE_IOS = 0.6

# FDOT confirmed on 2026-10-29 that the project may use FL511 with no end date, public
# display included. Kept as None (rather than removed) so a future permission that does
# carry a date has somewhere to go, and so anything that captures on this tool's behalf can
# refuse after it. This tool reads local files and never fetches.
EXPIRY_DATE: str | None = None

CSV_COLUMNS = ["captured_at", "camera_id", "image", "lane", "vehicle_count", *CLASS_ORDER]


def tile_windows(width: int, height: int, tile: int, overlap: float) -> list[tuple[int, int, int, int]]:
    """Cover `width` x `height` with `tile`-sized windows that overlap by `overlap`.

    Windows are half-open `(x0, y0, x1, y1)` and always inside the image: the last
    window in a row or column is pulled back so it ends exactly at the edge rather
    than running past it.
    """
    if tile <= 0:
        raise ValueError("tile must be positive")
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in [0, 1)")

    def starts(length: int) -> list[int]:
        if length <= tile:
            return [0]
        step = max(1, int(round(tile * (1 - overlap))))
        values = list(range(0, length - tile + 1, step))
        if values[-1] != length - tile:
            values.append(length - tile)
        return values

    return [
        (x0, y0, min(x0 + tile, width), min(y0 + tile, height))
        for y0 in starts(height)
        for x0 in starts(width)
    ]


def box_iou(a, b) -> float:
    """Intersection over union of two `(x1, y1, x2, y2)` boxes."""
    ax1, ay1, ax2, ay2 = a[:4]
    bx1, by1, bx2, by2 = b[:4]
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def merge_boxes(boxes, iou_threshold: float = DEFAULT_IOU):
    """Class-aware NMS over `(x1, y1, x2, y2, score, class_id)` boxes.

    Only boxes of the same class suppress each other, so a truck that is also
    detected as a bus in an overlapping tile keeps both candidates rather than
    silently losing one.
    """
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda box: float(box[4]), reverse=True)
    kept = []
    for candidate in ordered:
        if all(
            candidate[5] != other[5] or box_iou(candidate, other) < iou_threshold for other in kept
        ):
            kept.append(candidate)
    return kept


def box_ios(a, b) -> float:
    """Intersection over the smaller box's area.

    A vehicle cut by a tile edge shows up as a small fragment whose IoU with the
    full box is low, so plain NMS keeps both. IoS catches it: the fragment is
    almost entirely inside the full box.
    """
    ax1, ay1, ax2, ay2 = a[:4]
    bx1, by1, bx2, by2 = b[:4]
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    smaller = min(area_a, area_b)
    return intersection / smaller if smaller > 0 else 0.0


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def merge_fragments(boxes, ios_threshold: float = DEFAULT_MERGE_IOS):
    """Class-agnostic merge for tile-edge fragments and duplicate class pairs.

    Runs after the class-aware NMS. Two boxes that overlap by at least
    `ios_threshold` of the smaller one are the same vehicle: the larger geometry is
    kept, because a lane is assigned from the bottom-centre and a fragment's
    bottom-centre can fall in the wrong lane, while the class comes from the
    higher-scoring box. A threshold of 0 disables the merge.
    """
    if not boxes or ios_threshold <= 0:
        return list(boxes)
    ordered = sorted(boxes, key=lambda box: float(box[4]), reverse=True)
    kept = []
    for candidate in ordered:
        for index, other in enumerate(kept):
            if box_ios(candidate, other) < ios_threshold:
                continue
            larger = candidate if _area(candidate) > _area(other) else other
            better = candidate if float(candidate[4]) > float(other[4]) else other
            kept[index] = (larger[0], larger[1], larger[2], larger[3], better[4], better[5])
            break
        else:
            kept.append(candidate)
    return kept


def scale_polygon(polygon, from_size, to_size):
    """Scale `[[x, y], ...]` from the lanes file's frame size to the image size."""
    from_width, from_height = from_size
    to_width, to_height = to_size
    if from_width <= 0 or from_height <= 0:
        raise ValueError("frame_size must be positive")
    sx = to_width / from_width
    sy = to_height / from_height
    return [(float(x) * sx, float(y) * sy) for x, y in polygon]


def point_in_polygon(x: float, y: float, polygon) -> bool:
    """Ray casting; a point exactly on an edge counts as inside."""
    if len(polygon) < 3:
        return False
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        # On the edge (collinear and within the segment) counts as inside.
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if abs(cross) < 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            x_at_y = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_at_y:
                inside = not inside
    return inside


def load_lanes(path: Path) -> dict:
    """Read a lanes file, checking the fields this tool needs."""
    config = json.loads(path.read_text())
    if "lanes" not in config or not isinstance(config["lanes"], list):
        raise ValueError(f"{path} has no lanes list")
    for lane in config["lanes"]:
        if "id" not in lane or "polygon" not in lane:
            raise ValueError(f"{path}: every lane needs an id and a polygon")
        if len(lane["polygon"]) < 3:
            raise ValueError(f"{path}: lane {lane['id']} needs at least three polygon points")
    return config


def density_rows(
    detections,
    lanes_config: dict,
    image_size: tuple[int, int],
    *,
    camera_id: str,
    captured_at: str,
    image_name: str,
) -> list[dict]:
    """One row per lane: totals and per-class counts for this image."""
    lane_frame = lanes_config.get("frame_size") or list(image_size)
    rows = []
    for lane in lanes_config["lanes"]:
        polygon = scale_polygon(lane["polygon"], lane_frame, image_size)
        counts: Counter = Counter()
        for x1, y1, x2, y2, _score, class_id in detections:
            anchor_x = (x1 + x2) / 2
            anchor_y = y2  # bottom-centre, the same anchor the pipeline counts with
            if point_in_polygon(anchor_x, anchor_y, polygon):
                counts[VEHICLE_CLASSES.get(int(class_id), "other")] += 1
        row = {
            "captured_at": captured_at,
            "camera_id": camera_id,
            "image": image_name,
            "lane": lane["id"],
            "vehicle_count": sum(counts.values()),
        }
        for name in CLASS_ORDER:
            row[name] = counts.get(name, 0)
        rows.append(row)
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def detect_boxes(
    model,
    image: np.ndarray,
    *,
    imgsz: int = DEFAULT_IMGSZ,
    conf: float = DEFAULT_CONF,
    device=None,
    tile: int | None = DEFAULT_TILE,
    overlap: float = DEFAULT_OVERLAP,
    iou: float = DEFAULT_IOU,
    merge_ios: float = DEFAULT_MERGE_IOS,
):
    """Detect vehicles on the whole image plus overlapping tiles, merged."""
    height, width = image.shape[:2]
    # A tile smaller than the model input would be upscaled before detection, which is
    # what made tiled-1280 over-count; never let that happen.
    if tile:
        tile = max(tile, imgsz)
    raw = []

    def collect(prediction, dx: int, dy: int) -> None:
        boxes = prediction.boxes
        if boxes is None or len(boxes) == 0:
            return
        xyxy = boxes.xyxy.cpu().numpy()
        scores = boxes.conf.cpu().numpy()
        classes = boxes.cls.cpu().numpy()
        for index in range(len(xyxy)):
            x1, y1, x2, y2 = xyxy[index][:4]
            raw.append(
                (
                    float(x1) + dx,
                    float(y1) + dy,
                    float(x2) + dx,
                    float(y2) + dy,
                    float(scores[index]),
                    int(classes[index]),
                )
            )

    common = {"imgsz": imgsz, "conf": conf, "classes": list(VEHICLE_CLASSES), "verbose": False}
    if device:
        common["device"] = device

    for prediction in model.predict(source=image, **common):
        collect(prediction, 0, 0)

    if tile:
        for x0, y0, x1, y1 in tile_windows(width, height, tile, overlap):
            patch = image[y0:y1, x0:x1]
            if patch.size == 0:
                continue
            for prediction in model.predict(source=patch, **common):
                collect(prediction, x0, y0)

    return merge_fragments(merge_boxes(raw, iou_threshold=iou), ios_threshold=merge_ios)


def render_preview(image_path: Path, output_path: Path, boxes, lanes_config: dict) -> Path:
    """Draw the lane polygons and the detections, for checking a hand-drawn lanes file."""
    image = cv2.imread(str(image_path))
    if image is None:
        raise SystemExit(f"snapshot_density.py: cannot read image {image_path}")
    height, width = image.shape[:2]
    lane_frame = lanes_config.get("frame_size") or [width, height]

    for lane in lanes_config["lanes"]:
        polygon = np.array(
            scale_polygon(lane["polygon"], lane_frame, (width, height)), dtype=np.int32
        )
        overlay = image.copy()
        cv2.fillPoly(overlay, [polygon], (60, 120, 90))
        cv2.addWeighted(overlay, 0.25, image, 0.75, 0, image)
        cv2.polylines(image, [polygon], True, (255, 255, 255), 2)
        x, y = polygon[0]
        cv2.putText(image, f"Lane {lane['id']}", (int(x) + 6, int(y) + 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(image, f"Lane {lane['id']}", (int(x) + 6, int(y) + 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)

    for x1, y1, x2, y2, score, class_id in boxes:
        cv2.rectangle(image, (int(x1), int(y1)), (int(x2), int(y2)), (120, 255, 180), 2)
        label = f"{VEHICLE_CLASSES.get(int(class_id), 'other')} {score:.2f}"
        cv2.putText(image, label, (int(x1), max(12, int(y1) - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (120, 255, 180), 1, cv2.LINE_AA)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), image)
    return output_path


def captured_at_for(path: Path, override: str | None) -> str:
    """When the snapshot was retrieved, in UTC. FL511's own TIMESTAMP lags the image."""
    if override:
        return override
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Count vehicles per lane in FL511 snapshot images (density, not flow).",
        epilog="FL511 imagery used with FDOT's permission (confirmed 2026-10-29, no end "
        "date); capture images with 'python -m ingestion capture --camera-id <id> --once' "
        "and pass the files here.",
    )
    parser.add_argument("images", nargs="+", type=Path, help="Snapshot images to measure")
    parser.add_argument("--lanes", required=True, type=Path, help="Lanes file for this camera")
    parser.add_argument("--camera-id", required=True, help="Camera id recorded in every row")
    parser.add_argument("--output", type=Path, default=None, help="CSV path (default: data/output/<camera>_density.csv)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"YOLO weights (default: {DEFAULT_MODEL})")
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ, help=f"YOLO input size (default: {DEFAULT_IMGSZ})")
    parser.add_argument("--conf", type=float, default=DEFAULT_CONF, help=f"Minimum confidence (default: {DEFAULT_CONF})")
    parser.add_argument("--device", default=None, help="cpu to force CPU; default is whatever YOLO picks")
    parser.add_argument("--tile", type=int, default=DEFAULT_TILE, help=f"Tile size in pixels (default: {DEFAULT_TILE})")
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP, help=f"Tile overlap (default: {DEFAULT_OVERLAP})")
    parser.add_argument("--iou", type=float, default=DEFAULT_IOU, help=f"NMS IoU (default: {DEFAULT_IOU})")
    parser.add_argument(
        "--merge-ios",
        type=float,
        default=DEFAULT_MERGE_IOS,
        help=f"Class-agnostic merge: drop a box overlapping a better one by this share of the "
        f"smaller box (default: {DEFAULT_MERGE_IOS}; 0 disables)",
    )
    parser.add_argument("--no-tiles", action="store_true", help="Single full-image pass, for comparison")
    parser.add_argument(
        "--captured-at",
        default=None,
        help="UTC timestamp for a single image, e.g. 2026-10-29T14:03:00Z (default: file mtime)",
    )
    parser.add_argument("--preview", type=Path, default=None, help="Also write a JPEG of lanes and boxes")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.captured_at and len(args.images) > 1:
        raise SystemExit("snapshot_density.py: --captured-at applies to one image; omit it for a batch")

    try:
        lanes_config = load_lanes(args.lanes)
    except (OSError, ValueError) as error:
        raise SystemExit(f"snapshot_density.py: {error}") from error

    from model_source import resolve_model_cli

    model = None
    output = args.output or Path("data/output") / f"{args.camera_id}_density.csv"
    rows: list[dict] = []

    for image_path in args.images:
        image = cv2.imread(str(image_path))
        if image is None:
            raise SystemExit(f"snapshot_density.py: cannot read image {image_path}")
        if model is None:
            from ultralytics import YOLO

            model = YOLO(resolve_model_cli(args.model, "snapshot_density.py"))

        boxes = detect_boxes(
            model,
            image,
            imgsz=args.imgsz,
            conf=args.conf,
            device=args.device,
            tile=None if args.no_tiles else args.tile,
            overlap=args.overlap,
            iou=args.iou,
            merge_ios=args.merge_ios,
        )
        height, width = image.shape[:2]
        captured_at = captured_at_for(image_path, args.captured_at)
        image_rows = density_rows(
            boxes,
            lanes_config,
            (width, height),
            camera_id=args.camera_id,
            captured_at=captured_at,
            image_name=image_path.name,
        )
        rows.extend(image_rows)

        if args.preview and len(args.images) == 1:
            render_preview(image_path, args.preview, boxes, lanes_config)
            print(f"Wrote preview to {args.preview}")

        print(f"{image_path.name}: {len(boxes)} boxes, {sum(r['vehicle_count'] for r in image_rows)} in lanes")

    write_csv(rows, output)
    print(f"Wrote {len(rows)} rows to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
