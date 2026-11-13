"""Headlight mode: find vehicles at night from their lights alone.

When a camera switches to its dark-scene mode, vehicles are bright headlight blobs on a
black road, and a COCO-trained YOLO model finds nothing. This module detects them as
bright, compact, *moving* blobs and turns each into a box that the same tracker and
lane-crossing counter as daytime can use:

1. A background model (MOG2) learns what doesn't move: street lights, signs, the
   camera's text overlay.
2. Pixels that are both very bright (a saturated headlight core) and moving are kept.
3. A short horizontal closing joins a car's two headlights into one blob.
4. Each blob big enough to be a vehicle becomes one vehicle-sized box, grown upward from
   the lights so consecutive frames overlap for the tracker, with class "car" (lights alone
   can't tell a car from a truck).

Tracking uses Ultralytics' BYTETracker, the same tracker as daytime, configured from
the same tracker YAML. See pipeline/README.md, "Night: headlight mode".
"""

from __future__ import annotations

import cv2
import numpy as np
from ultralytics.engine.results import Boxes
from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.utils import YAML, IterableSimpleNamespace
from ultralytics.utils.checks import check_yaml

# COCO class used for every headlight detection: lights can't tell vehicle types apart.
HEADLIGHT_CLASS_ID = 2  # car
# All headlight boxes get the same score, above BYTETracker's high threshold.
HEADLIGHT_SCORE = 0.9


class HeadlightDetector:
    """Bright, moving, compact blobs -> vehicle boxes, one frame at a time.

    Sizes are given for a 640 px wide frame and scaled to the actual width, so the same
    settings work for 640x480 and 720x480 Caltrans streams and larger videos.
    """

    def __init__(
        self,
        core_threshold: int = 230,
        min_area: float = 12.0,
        merge_width: float = 14.0,
        warmup_frames: int = 30,
        history: int = 300,
        min_box_width: float = 12.0,
        box_aspect: float = 0.8,
    ):
        self.core_threshold = core_threshold
        self.min_area = min_area
        self.merge_width = merge_width
        self.min_box_width = min_box_width
        self.box_aspect = box_aspect
        self.warmup_frames = warmup_frames
        self.background = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=25, detectShadows=False
        )
        self.frames_seen = 0

    def detect(self, frame: np.ndarray) -> np.ndarray:
        """Return an (N, 6) array of [x1, y1, x2, y2, score, class_id] for one frame."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        moving = self.background.apply(gray)
        self.frames_seen += 1
        if self.frames_seen <= self.warmup_frames:
            # The background model marks everything as moving until it has learned the scene.
            return np.zeros((0, 6), dtype=np.float32)

        scale = gray.shape[1] / 640.0
        moving = cv2.dilate(moving, np.ones((5, 5), np.uint8))
        cores = (gray >= self.core_threshold).astype(np.uint8) * 255
        lights = cv2.bitwise_and(cores, moving)

        # Join a car's two headlights (side by side) into one blob.
        width = max(3, int(round(self.merge_width * scale)))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (width, max(3, width // 3)))
        lights = cv2.morphologyEx(lights, cv2.MORPH_CLOSE, kernel)

        count, _, stats, _ = cv2.connectedComponentsWithStats(lights, connectivity=8)
        min_area = self.min_area * scale * scale
        min_width = self.min_box_width * scale
        boxes = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if area < min_area:
                continue
            # A pair of lights is a thin strip; between frames, a car moves farther than the
            # strip is tall, so strips from consecutive frames don't overlap and an
            # overlap-based tracker loses the car. Grow the box to vehicle proportions,
            # upward from the lights: the bottom edge (where lanes and counting lines look)
            # stays at the lights.
            box_w = max(float(w), min_width)
            box_h = max(float(h), self.box_aspect * box_w)
            cx, bottom = x + w / 2.0, float(y + h)
            boxes.append((cx - box_w / 2, bottom - box_h, cx + box_w / 2, bottom, HEADLIGHT_SCORE, HEADLIGHT_CLASS_ID))
        return np.array(boxes, dtype=np.float32).reshape(-1, 6)


def make_tracker(tracker_yaml: str = "bytetrack.yaml") -> BYTETracker:
    """The same BYTETracker configuration the daytime path uses."""
    config = IterableSimpleNamespace(**YAML.load(check_yaml(tracker_yaml)))
    return BYTETracker(config)


def track(tracker: BYTETracker, detections: np.ndarray, frame_shape) -> np.ndarray:
    """Run one tracker step. Returns (N, 7) [x1, y1, x2, y2, track_id, score, class_id]."""
    boxes = Boxes(detections, orig_shape=frame_shape[:2])
    tracked = tracker.update(boxes)
    if len(tracked) == 0:
        return np.zeros((0, 7), dtype=np.float32)
    return np.asarray(tracked[:, :7], dtype=np.float32)
