"""Run pretrained YOLO on a video and save an annotated copy with vehicle boxes.

This is a quick check that detection works on our footage before adding
tracking and counting (see track_count.py).

Usage:
    python pipeline/detect.py data/sample.mp4
    python pipeline/detect.py data/sample.mp4 --device cpu --conf 0.4
"""

import argparse
import time
from collections import Counter
from pathlib import Path

import cv2
from ultralytics import YOLO

from model_source import resolve_model_cli

# COCO class IDs for the vehicle classes in the events.csv schema
VEHICLE_CLASSES = {1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def parse_args():
    parser = argparse.ArgumentParser(description="Detect vehicles in a video with YOLO.")
    parser.add_argument("source", type=Path, help="Path to the input video")
    parser.add_argument("--model", default="yolo26m.pt", help="YOLO weights (default: yolo26m.pt)")
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
        help="Annotated video path (default: data/output/<name>_detect.mp4)",
    )
    parser.add_argument("--show", action="store_true", help="Show a live preview window (press q to stop)")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Video not found: {args.source}")

    output = args.output or Path("data/output") / f"{args.source.stem}_detect.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(args.source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    model = YOLO(resolve_model_cli(args.model, "detect.py"))

    # Detections summed over all frames. One car seen for 100 frames counts
    # 100 times here; counting unique vehicles needs tracking (track_count.py).
    detections = Counter()
    frames = 0
    start = time.perf_counter()

    results = model.predict(
        source=str(args.source),
        classes=list(VEHICLE_CLASSES),
        conf=args.conf,
        imgsz=args.imgsz,
        device=args.device,
        stream=True,
        verbose=False,
    )
    for result in results:
        frames += 1
        for cls_id in result.boxes.cls.int().tolist():
            detections[VEHICLE_CLASSES[cls_id]] += 1

        annotated = result.plot()
        writer.write(annotated)

        if args.show:
            cv2.imshow("detect", annotated)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        if frames % 100 == 0:
            print(f"  frame {frames}/{total_frames}")

    writer.release()
    cv2.destroyAllWindows()
    elapsed = time.perf_counter() - start

    print(f"\nProcessed {frames} frames in {elapsed:.1f}s ({frames / elapsed:.1f} FPS) on {model.predictor.device}")
    print(f"Saved annotated video to {output}")
    print("Detections per class (summed over frames, not unique vehicles):")
    for name in VEHICLE_CLASSES.values():
        print(f"  {name:<11} {detections[name]}")


if __name__ == "__main__":
    main()
