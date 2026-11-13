import sys
import unittest
from pathlib import Path

import numpy as np
import supervision as sv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane_assign import LaneAreas, widened_line  # noqa: E402

W, H = 400, 300
# Three side-by-side lanes, each 100 px wide, from x=50 to x=350.
LANES = [np.array([[50 + 100 * i, 0], [150 + 100 * i, 0], [150 + 100 * i, H], [50 + 100 * i, H]]) for i in range(3)]


class MajorityTests(unittest.TestCase):
    def setUp(self):
        self.areas = LaneAreas(LANES, W, H)

    def test_a_box_inside_one_lane_is_all_in_it(self):
        shares = self.areas.shares(np.array([[160, 100, 240, 180]]))
        np.testing.assert_allclose(shares[0], [0, 1, 0], atol=0.02)

    def test_the_lane_with_most_of_the_box_wins(self):
        box = np.array([[120, 50, 220, 200]])  # 30 px wide in lane 0, 70 px in lane 1
        self.assertEqual(self.areas.majority(box).tolist(), [1])

    def test_on_slanted_lanes_the_area_decides_not_the_bottom_centre(self):
        # Perspective leans lanes: each one is 100 px further left at the top of the frame
        # than at the bottom, so the lane 0 / lane 1 boundary runs from x=50 at the top to
        # x=150 at the bottom. A tall headlight box grown upward from lights just left of
        # that boundary has its bottom-centre in lane 0, but most of its area higher up,
        # where lane 1 has moved under it.
        slanted = [
            np.array([[-50 + 100 * i, 0], [50 + 100 * i, 0], [150 + 100 * i, H], [50 + 100 * i, H]])
            for i in range(2)
        ]
        areas = LaneAreas(slanted, W, H)
        box = np.array([[100, 0, 190, H]])
        bottom_centre_x = (100 + 190) / 2  # 145 at the bottom, where lane 0 is x 50..150
        self.assertLess(bottom_centre_x, 150)
        self.assertEqual(areas.majority(box).tolist(), [1])

    def test_a_box_off_the_road_has_no_lane(self):
        self.assertEqual(self.areas.majority(np.array([[0, 0, 45, 40], [355, 10, 399, 60]])).tolist(), [-1, -1])

    def test_a_box_mostly_outside_the_frame_is_judged_by_its_whole_area(self):
        # Only a sliver of this box is in the picture, inside lane 0.
        self.assertEqual(self.areas.majority(np.array([[60, -400, 140, 10]])).tolist(), [-1])

    def test_no_boxes_and_no_lanes(self):
        self.assertEqual(self.areas.majority(np.zeros((0, 4))).tolist(), [])
        self.assertEqual(LaneAreas([], W, H).majority(np.array([[1, 1, 5, 5]])).tolist(), [-1])


class WidenedLineTests(unittest.TestCase):
    def test_reaches_the_frame_edges_and_keeps_direction(self):
        (ax, ay), (bx, by) = widened_line((150, 200), (250, 200), W, H)
        self.assertEqual((ax, ay, bx, by), (0, 200, W - 1, 200))
        (ax, _), (bx, _) = widened_line((250, 200), (150, 200), W, H)
        self.assertGreater(ax, bx)  # still runs the way the lane's line ran

    def test_a_slanted_line(self):
        (ax, ay), (bx, by) = widened_line((100, 100), (200, 150), W, H)
        self.assertEqual((ax, ay), (0, 50))
        self.assertEqual(bx, W - 1)
        self.assertAlmostEqual(by, 50 + (W - 1) / 2, delta=1)

    def test_counts_a_crossing_the_short_line_would_miss(self):
        # A box whose bottom-centre is at x=60, left of the lane's own line (x 150..250).
        short = sv.LineZone(sv.Point(150, 200), sv.Point(250, 200), triggering_anchors=(sv.Position.BOTTOM_CENTER,))
        p1, p2 = widened_line((150, 200), (250, 200), W, H)
        wide = sv.LineZone(sv.Point(*p1), sv.Point(*p2), triggering_anchors=(sv.Position.BOTTOM_CENTER,))
        crossed = {"short": 0, "wide": 0}
        for y in (150, 180, 220, 250):
            det = sv.Detections(xyxy=np.array([[40.0, y - 60.0, 80.0, float(y)]]), tracker_id=np.array([7]))
            for name, zone in (("short", short), ("wide", wide)):
                a, b = zone.trigger(det)
                crossed[name] += int(a.sum() + b.sum())
        self.assertEqual(crossed, {"short": 0, "wide": 1})


if __name__ == "__main__":
    unittest.main()
