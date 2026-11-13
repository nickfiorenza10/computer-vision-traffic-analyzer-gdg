import sys
import unittest
from pathlib import Path

import numpy as np
import supervision as sv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_lanes import build_lanes  # noqa: E402

W, H = 640, 480
VANISHING = (320.0, -100.0)
BOTTOM = 470.0
CAR_AT_BOTTOM = 40.0  # px wide; lanes are 80 px apart at the bottom, two car widths


def perspective_path(bottom_x, *, outbound=True, start=460.0, end=150.0, steps=30, jitter=0.0, seed=0):
    """A car driving straight along a lane that converges on the vanishing point."""
    rng = np.random.default_rng(seed)
    ys = np.linspace(start, end, steps) if outbound else np.linspace(end, start, steps)
    depth = (ys - VANISHING[1]) / (BOTTOM - VANISHING[1])
    xs = VANISHING[0] + (bottom_x - VANISHING[0]) * depth + rng.normal(0, jitter, steps)
    widths = CAR_AT_BOTTOM * depth
    return np.column_stack([xs, ys, widths, 0.75 * widths])


def horizontal_path(y, *, rightward=True, steps=30, seed=0):
    xs = np.linspace(60, 580, steps) if rightward else np.linspace(580, 60, steps)
    ys = np.full(steps, y) + np.random.default_rng(seed).normal(0, 1, steps)
    return np.column_stack([xs, ys, np.full(steps, 50.0), np.full(steps, 30.0)])


def tracks_of(paths):
    return {i: p for i, p in enumerate(paths)}


class ConvergingRoadTests(unittest.TestCase):
    def setUp(self):
        # Three lanes going away from the camera, two coming toward it, four cars each.
        paths = []
        for lane_x in (120, 200, 280):
            paths += [perspective_path(lane_x, jitter=2, seed=s) for s in range(4)]
        for lane_x in (400, 480):
            paths += [perspective_path(lane_x, outbound=False, jitter=2, seed=10 + s) for s in range(4)]
        self.lanes, self.info = build_lanes(tracks_of(paths), W, H, min_tracks=2)

    def test_one_lane_per_direction_holding_all_its_traffic(self):
        self.assertEqual(len(self.lanes), 2)
        self.assertEqual(self.info["flows"], 2)
        self.assertEqual(sorted(lane["vehicles_seen"] for lane in self.lanes), [8, 12])
        self.assertTrue(self.info["quality"]["approved"], self.info["quality"]["problems"])

    def test_each_direction_covers_its_outer_lanes(self):
        # At the bottom of the frame the away-going lanes run x 120..280: the polygon must
        # reach past both, so no car in an outer lane falls outside it.
        away = next(lane for lane in self.lanes if 120 < np.mean([p[0] for p in lane["polygon"]]) < 300)
        bottom = sorted(away["polygon"], key=lambda p: -p[1])[:2]
        xs = sorted(p[0] for p in bottom)
        self.assertLess(xs[0], 120 - 5)
        self.assertGreater(xs[1], 280 + 5)

    def test_lanes_follow_the_perspective_instead_of_overlapping(self):
        # Each lane's polygon narrows toward the horizon like the road does.
        for lane in self.lanes:
            polygon = np.array(lane["polygon"])
            far_width = np.hypot(*(polygon[0] - polygon[1])) if polygon[0][1] < polygon[2][1] else None
            near_width = np.hypot(*(polygon[2] - polygon[3]))
            if far_width is not None:
                self.assertLess(far_width, near_width)

    def test_a_car_driving_its_lane_is_counted_in_its_own_direction(self):
        for lane in self.lanes:
            (x1, y1), (x2, y2) = lane["line"]
            zone = sv.LineZone(sv.Point(x1, y1), sv.Point(x2, y2), triggering_anchors=(sv.Position.CENTER,))
            middle = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
            going_away = lane["directions"]["in"] == "outbound" or lane["directions"]["out"] == "inbound"
            # Which way this lane's traffic really moves: away (up) for the left three.
            up = middle[0] < VANISHING[0] + 20
            step = np.array([0.0, -6.0 if up else 6.0])
            result = None
            for k in (-1, 1):
                x, y = middle + k * step
                det = sv.Detections(xyxy=np.array([[x - 1, y - 1, x + 1, y + 1]]), tracker_id=np.array([1]))
                result = zone.trigger(det)
            crossed_in, crossed_out = result
            label = lane["directions"]["in"] if crossed_in.any() else lane["directions"]["out"]
            self.assertTrue(crossed_in.any() or crossed_out.any())
            self.assertEqual(label, "outbound" if up else "inbound", lane)
            self.assertEqual(going_away, up)


class RejectionTests(unittest.TestCase):
    def test_an_intersection_is_not_approved(self):
        paths = [perspective_path(x, jitter=2, seed=s) for x in (200, 280) for s in range(4)]
        paths += [horizontal_path(y, seed=20 + s) for y in (300, 340) for s in range(4)]
        lanes, info = build_lanes(tracks_of(paths), W, H, min_tracks=2)
        self.assertFalse(info["quality"]["approved"])
        self.assertTrue(any("intersection" in p for p in info["quality"]["problems"]))
        self.assertGreaterEqual(len(lanes), 2)

    def test_a_direction_seen_by_two_cars_needs_checking(self):
        paths = [perspective_path(200, jitter=2, seed=s) for s in range(4)]
        paths += [perspective_path(440, outbound=False, jitter=2, seed=30 + s) for s in range(2)]
        _, info = build_lanes(tracks_of(paths), W, H, min_tracks=2)
        self.assertFalse(info["quality"]["approved"])
        self.assertTrue(any("only 2 vehicles" in p for p in info["quality"]["problems"]))

    def test_turning_and_parked_vehicles_make_no_lanes(self):
        paths = [perspective_path(200, jitter=2, seed=s) for s in range(4)]
        # A U-turn: goes up and comes back, so it is not straight.
        up = perspective_path(320, steps=15)
        paths.append(np.vstack([up, up[::-1]]))
        # A parked car: barely moves.
        paths.append(np.column_stack([np.full(30, 500.0), np.full(30, 400.0), np.full(30, 40.0), np.full(30, 30.0)]))
        lanes, info = build_lanes(tracks_of(paths), W, H, min_tracks=2)
        self.assertEqual(info["paths"], 4)
        self.assertEqual(len(lanes), 1)


if __name__ == "__main__":
    unittest.main()
