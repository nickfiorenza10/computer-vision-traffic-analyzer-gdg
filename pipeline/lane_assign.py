"""Which lane a vehicle box belongs to, when its bottom-centre cannot be trusted.

By day a box hugs the vehicle, so its bottom-centre is where the tyres meet the road and
`sv.PolygonZone` with that anchor puts it in the right lane. At night the headlight
detector (headlights.py) grows a synthetic box upward from a pair of lights, and the
bottom-centre of that box can sit over the next lane. So for headlight boxes a vehicle
belongs to the lane that holds the largest share of the box's area.

Two consequences for counting:

* Membership: `LaneAreas.majority` rasterises each lane polygon once and answers every box
  with four lookups in an integral image, so it costs nothing per frame.
* Crossing: `sv.LineZone` ignores anchors that fall outside its segment's ends, which a
  box assigned by area can do. `widened_line` stretches a lane's counting line to the frame
  edges, keeping its direction, so "in" and "out" mean what they meant in lanes.json.
"""

from __future__ import annotations

import cv2
import numpy as np
import supervision as sv

# A box with less than this share in every lane is not on the road at all (a reflection,
# a light on a sign). A quarter tolerates a box that straddles three lanes.
MIN_SHARE = 0.25


class LaneAreas:
    """Each lane polygon as an integral image, for box-area questions."""

    def __init__(self, polygons: list[np.ndarray], width: int, height: int):
        self.width = width
        self.height = height
        self._integrals = []
        for polygon in polygons:
            mask = np.zeros((height, width), dtype=np.uint8)
            cv2.fillPoly(mask, [np.asarray(polygon, dtype=np.int32).reshape(-1, 1, 2)], 1)
            self._integrals.append(cv2.integral(mask))

    def shares(self, boxes: np.ndarray) -> np.ndarray:
        """(boxes x lanes): the fraction of each box's area inside each lane."""
        boxes = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)
        if not len(boxes) or not self._integrals:
            return np.zeros((len(boxes), len(self._integrals)))
        x1 = np.clip(np.floor(boxes[:, 0]), 0, self.width).astype(int)
        y1 = np.clip(np.floor(boxes[:, 1]), 0, self.height).astype(int)
        x2 = np.clip(np.ceil(boxes[:, 2]), 0, self.width).astype(int)
        y2 = np.clip(np.ceil(boxes[:, 3]), 0, self.height).astype(int)
        # The whole box, including any part outside the frame, is the denominator: a box
        # mostly off-screen should not count as mostly in a lane.
        full = np.maximum(1.0, (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]))
        out = np.empty((len(boxes), len(self._integrals)))
        for j, integral in enumerate(self._integrals):
            inside = integral[y2, x2] - integral[y1, x2] - integral[y2, x1] + integral[y1, x1]
            out[:, j] = inside / full
        return out

    def majority(self, boxes: np.ndarray, min_share: float = MIN_SHARE) -> np.ndarray:
        """Each box's lane index (into the polygons given), or -1 when it is on no lane."""
        shares = self.shares(boxes)
        if not shares.size:
            return np.full(len(shares), -1, dtype=int)
        best = shares.argmax(axis=1)
        return np.where(shares[np.arange(len(best)), best] >= min_share, best, -1)


class MajorityLanes:
    """Lane membership by area, and a frame-wide counting line per lane, for headlight boxes.

    `lanes` are the dicts `track_count.load_lanes` builds (`polygon`, `line_points`). Each
    lane keeps its own LineZone here, so the crossing history of the day's narrow lines and
    these wide ones never mix.
    """

    def __init__(self, lanes: list[dict], width: int, height: int):
        self.areas = LaneAreas([lane["polygon"] for lane in lanes], width, height)
        self.lines = []
        for lane in lanes:
            p1, p2 = widened_line(*lane["line_points"], width, height)
            self.lines.append(
                sv.LineZone(sv.Point(*p1), sv.Point(*p2), triggering_anchors=(sv.Position.BOTTOM_CENTER,))
            )

    def members(self, detections: sv.Detections) -> list[np.ndarray]:
        """For each lane, which detections belong to it (each detection to one lane at most)."""
        owner = self.areas.majority(detections.xyxy)
        return [owner == index for index in range(len(self.lines))]


def widened_line(
    p1: tuple[float, float], p2: tuple[float, float], width: int, height: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """The line through p1 and p2, clipped to the frame, still running from p1's side to p2's."""
    (x1, y1), (x2, y2) = (float(v) for v in p1), (float(v) for v in p2)
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return (round(x1), round(y1)), (round(x2), round(y2))
    # Parameters t where the infinite line meets each frame edge; keep the two that land
    # on the frame, lowest first, so the direction is unchanged.
    ts = []
    if dx:
        ts += [(0 - x1) / dx, (width - 1 - x1) / dx]
    if dy:
        ts += [(0 - y1) / dy, (height - 1 - y1) / dy]
    on_frame = []
    for t in ts:
        x, y = x1 + t * dx, y1 + t * dy
        if -0.5 <= x <= width - 0.5 and -0.5 <= y <= height - 0.5:
            on_frame.append(t)
    if len(on_frame) < 2:
        return (round(x1), round(y1)), (round(x2), round(y2))
    lo, hi = min(on_frame), max(on_frame)
    return (
        (round(x1 + lo * dx), round(y1 + lo * dy)),
        (round(x1 + hi * dx), round(y1 + hi * dy)),
    )
