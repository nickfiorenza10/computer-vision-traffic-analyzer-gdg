"""Count vehicles by hand to measure the pipeline's accuracy.

Plays a video with the lanes and counting lines drawn on it. Press a lane's number
each time a vehicle crosses that lane's line. The counts go to a CSV that
compare_counts.py checks against track_count.py's events.csv.

Usage:
    python pipeline/hand_count.py data/vehicles-2.mp4 --lanes pipeline/lanes_vehicles-2.json
    python pipeline/hand_count.py data/vehicles-2.mp4 --lanes pipeline/lanes_vehicles-2.json --start-frame 300 --end-frame 600

Keys:
    1-9      count a vehicle crossing that lane, at the current frame
    space    pause / play
    d / a    step one frame forward / back (pauses playback)
    ] / [    play faster / slower
    u        undo the last count
    s        save
    q / Esc  save and quit

Running it again with the same --output picks up where you left off.
"""

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

SPEEDS = [0.25, 0.5, 1.0, 1.5, 2.0]
KEY_SPACE, KEY_ESC = 32, 27


def parse_args():
    parser = argparse.ArgumentParser(description="Count vehicle crossings by hand.")
    parser.add_argument("source", type=Path, help="Path to the video")
    parser.add_argument("--lanes", type=Path, required=True, help="Lanes file for this camera")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Hand-count CSV (default: data/output/<name>_hand.csv)",
    )
    parser.add_argument("--start-frame", type=int, default=0, help="First frame to count from (default: 0)")
    parser.add_argument("--end-frame", type=int, default=None, help="Stop at this frame (default: end of video)")
    return parser.parse_args()


@dataclass
class HandCounter:
    """Keyboard handling and the count log, kept free of any window code so it can be tested."""

    lane_ids: list
    start_frame: int
    end_frame: int
    frame: int = 0
    playing: bool = False
    speed_index: int = SPEEDS.index(1.0)
    counts: list = field(default_factory=list)  # [(frame, lane), ...] in the order pressed
    furthest_frame: int = 0  # the last frame reviewed, saved so comparisons use the right window
    dirty: bool = False

    def __post_init__(self):
        self.frame = max(self.frame, self.start_frame)
        self.furthest_frame = max(self.furthest_frame, self.frame)

    @property
    def speed(self):
        return SPEEDS[self.speed_index]

    def seek(self, frame):
        self.frame = min(max(frame, self.start_frame), self.end_frame)
        self.furthest_frame = max(self.furthest_frame, self.frame)

    def handle_key(self, key):
        """Apply one key press. Returns "quit", "save" or None."""
        if key in (ord("q"), KEY_ESC):
            return "quit"
        if key == ord("s"):
            return "save"
        if key == KEY_SPACE:
            self.playing = not self.playing
        elif key == ord("d"):
            self.playing = False
            self.seek(self.frame + 1)
        elif key == ord("a"):
            self.playing = False
            self.seek(self.frame - 1)
        elif key == ord("]"):
            self.speed_index = min(self.speed_index + 1, len(SPEEDS) - 1)
        elif key == ord("["):
            self.speed_index = max(self.speed_index - 1, 0)
        elif key == ord("u"):
            if self.counts:
                self.counts.pop()
                self.dirty = True
        elif ord("1") <= key <= ord("9"):
            lane = key - ord("0")
            if lane in self.lane_ids:
                self.counts.append((self.frame, lane))
                self.dirty = True
        return None

    def tally(self):
        totals = {lane: 0 for lane in self.lane_ids}
        for _, lane in self.counts:
            totals[lane] += 1
        return totals


def load_lanes(path, width, height):
    """Lanes scaled to the video's resolution, like track_count.py does."""
    config = json.loads(path.read_text())
    lane_w, lane_h = config.get("frame_size", (width, height))
    sx, sy = width / lane_w, height / lane_h

    def scale(points):
        return np.array([[round(x * sx), round(y * sy)] for x, y in points], dtype=np.int32)

    lanes = [{"id": lane["id"], "polygon": scale(lane["polygon"]), "line": scale(lane["line"])} for lane in config["lanes"]]
    if any(not 1 <= lane["id"] <= 9 for lane in lanes):
        raise SystemExit("hand_count.py supports lane IDs 1-9 (one key per lane)")
    return lanes


def sidecar_path(output):
    return output.with_suffix(".json")


def load_existing(output):
    """Counts and the reviewed window from an earlier session, if any."""
    if not output.is_file():
        return [], None
    with output.open(newline="") as f:
        counts = [(int(row["frame"]), int(row["lane"])) for row in csv.DictReader(f)]
    meta = json.loads(sidecar_path(output).read_text()) if sidecar_path(output).is_file() else None
    return counts, meta


def save(counter, output, source, lanes_path, fps):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "seconds", "lane"])
        for frame, lane in sorted(counter.counts):
            writer.writerow([frame, round(frame / fps, 3), lane])
    sidecar_path(output).write_text(
        json.dumps(
            {
                "video": source.name,
                "lanes": str(lanes_path),
                "fps": fps,
                "start_frame": counter.start_frame,
                "end_frame": counter.furthest_frame,
                "counts": len(counter.counts),
            },
            indent=2,
        )
    )
    counter.dirty = False


def draw(frame_image, lanes, counter, fps):
    image = frame_image.copy()
    totals = counter.tally()
    for lane in lanes:
        cv2.polylines(image, [lane["polygon"].reshape(-1, 1, 2)], True, (255, 200, 0), 2)
        (x1, y1), (x2, y2) = lane["line"]
        cv2.line(image, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 3)
        cv2.putText(
            image, f"{lane['id']}: {totals[lane['id']]}", (int(x1), int(y1) - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2,
        )
    # Flash the lane number for a few frames after a count, so presses are visible
    recent = [lane for frame, lane in counter.counts if 0 <= counter.frame - frame < 5]
    state = "PLAY" if counter.playing else "PAUSED"
    status = (
        f"frame {counter.frame}/{counter.end_frame}  {counter.frame / fps:6.2f}s  {state} {counter.speed}x  "
        f"counted {len(counter.counts)}" + (f"  +lane {recent[-1]}" if recent else "")
    )
    cv2.rectangle(image, (0, 0), (image.shape[1], 36), (0, 0, 0), -1)
    cv2.putText(image, status, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    return image


def main():
    args = parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Video not found: {args.source}")
    output = args.output or Path("data/output") / f"{args.source.stem}_hand.csv"

    cap = cv2.VideoCapture(str(args.source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    lanes = load_lanes(args.lanes, width, height)
    end_frame = min(args.end_frame if args.end_frame is not None else total - 1, total - 1)

    counts, meta = load_existing(output)
    counter = HandCounter([lane["id"] for lane in lanes], args.start_frame, end_frame, counts=counts)
    if meta:
        if meta.get("start_frame") != args.start_frame:
            raise SystemExit(
                f"{output} was started at frame {meta.get('start_frame')}; use the same --start-frame or another --output"
            )
        counter.furthest_frame = meta["end_frame"]
        counter.seek(meta["end_frame"])
        print(f"Resuming {output}: {len(counts)} counts, at frame {counter.frame}")

    # Scale large videos to fit the screen; counting uses frame numbers, not pixels
    scale = min(1.0, 1600 / width, 900 / height)
    window = "hand count (1-9 count, space pause, a/d step, [ ] speed, u undo, s save, q quit)"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)

    shown = None
    image = None
    while True:
        if shown != counter.frame:
            if shown is None or counter.frame != shown + 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, counter.frame)
            ok, image = cap.read()
            if not ok:
                break
            shown = counter.frame
        view = draw(image, lanes, counter, fps)
        if scale < 1.0:
            view = cv2.resize(view, None, fx=scale, fy=scale)
        cv2.imshow(window, view)

        delay = max(1, int(1000 / (fps * counter.speed))) if counter.playing else 50
        action = counter.handle_key(cv2.waitKey(delay) & 0xFF)
        if action == "quit":
            break
        if action == "save":
            save(counter, output, args.source, args.lanes, fps)
            print(f"Saved {len(counter.counts)} counts to {output}")
        if counter.playing:
            if counter.frame >= counter.end_frame:
                counter.playing = False
            else:
                counter.seek(counter.frame + 1)

    save(counter, output, args.source, args.lanes, fps)
    cap.release()
    cv2.destroyAllWindows()
    print(f"Saved {len(counter.counts)} counts (frames {counter.start_frame}-{counter.furthest_frame}) to {output}")
    for lane, n in counter.tally().items():
        print(f"  lane {lane}: {n}")


if __name__ == "__main__":
    main()
