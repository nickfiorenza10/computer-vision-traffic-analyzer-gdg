import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapshot_density import (  # noqa: E402
    CSV_COLUMNS,
    box_ios,
    box_iou,
    density_rows,
    merge_boxes,
    merge_fragments,
    point_in_polygon,
    scale_polygon,
    tile_windows,
    write_csv,
)


class TileWindowTests(unittest.TestCase):
    def test_small_image_is_one_window(self):
        self.assertEqual(tile_windows(400, 300, 640, 0.2), [(0, 0, 400, 300)])

    def test_windows_stay_inside_and_reach_the_edges(self):
        for window in tile_windows(1920, 1080, 640, 0.2):
            x0, y0, x1, y1 = window
            self.assertGreaterEqual(x0, 0)
            self.assertGreaterEqual(y0, 0)
            self.assertLessEqual(x1, 1920)
            self.assertLessEqual(y1, 1080)
        xs = {window[0] for window in tile_windows(1920, 1080, 640, 0.2)}
        ys = {window[1] for window in tile_windows(1920, 1080, 640, 0.2)}
        self.assertIn(0, xs)
        self.assertIn(1920 - 640, xs)
        self.assertIn(0, ys)
        self.assertIn(1080 - 640, ys)

    def test_every_pixel_is_covered(self):
        width, height, tile, overlap = 300, 200, 96, 0.25
        windows = tile_windows(width, height, tile, overlap)
        for y in range(0, height, 7):
            for x in range(0, width, 7):
                self.assertTrue(
                    any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in windows),
                    f"pixel {(x, y)} is in no tile",
                )

    def test_overlap_produces_more_windows_than_no_overlap(self):
        self.assertGreater(
            len(tile_windows(1000, 1000, 400, 0.3)),
            len(tile_windows(1000, 1000, 400, 0.0)),
        )

    def test_bad_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            tile_windows(100, 100, 0, 0.2)
        with self.assertRaises(ValueError):
            tile_windows(100, 100, 50, 1.0)
        with self.assertRaises(ValueError):
            tile_windows(100, 100, 50, -0.1)


class MergeBoxTests(unittest.TestCase):
    def test_duplicates_of_the_same_class_keep_the_better_score(self):
        boxes = [
            (10, 10, 50, 50, 0.4, 2),
            (11, 11, 51, 51, 0.9, 2),
        ]
        merged = merge_boxes(boxes, iou_threshold=0.5)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0][4], 0.9)

    def test_same_place_different_class_is_kept(self):
        boxes = [
            (10, 10, 50, 50, 0.8, 2),  # car
            (10, 10, 50, 50, 0.7, 7),  # truck
        ]
        self.assertEqual(len(merge_boxes(boxes, iou_threshold=0.5)), 2)

    def test_separate_boxes_are_both_kept(self):
        boxes = [
            (0, 0, 20, 20, 0.9, 2),
            (200, 200, 240, 240, 0.5, 2),
        ]
        self.assertEqual(len(merge_boxes(boxes, iou_threshold=0.5)), 2)

    def test_empty_input(self):
        self.assertEqual(merge_boxes([], iou_threshold=0.5), [])

    def test_iou_of_disjoint_boxes_is_zero(self):
        self.assertEqual(box_iou((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)


class MergeFragmentTests(unittest.TestCase):
    """Tile-edge fragments and car/truck pairs on one vehicle, both class-agnostic."""

    def test_fragment_inside_a_full_box_is_dropped(self):
        full = (100, 100, 200, 200, 0.80, 2)
        fragment = (100, 100, 200, 140, 0.45, 2)  # tile edge cut the bottom off
        merged = merge_fragments([full, fragment], ios_threshold=0.6)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][4], 0.80)

    def test_a_high_scoring_fragment_keeps_the_larger_geometry(self):
        # A tile edge cut the car in half, but the half scored higher. The lane comes
        # from the bottom-centre, so the full box's geometry has to survive, while the
        # class comes from the better score.
        full = (100, 100, 200, 200, 0.30, 2)  # car, lower score
        fragment = (100, 100, 200, 160, 0.75, 7)  # top half, higher score, labelled truck
        merged = merge_fragments([full, fragment], ios_threshold=0.6)
        self.assertEqual(len(merged), 1)
        box = merged[0]
        self.assertEqual(box[:4], full[:4])  # full-height geometry
        self.assertAlmostEqual(box[4], 0.75)  # better score
        self.assertEqual(box[5], 7)  # class from the better-scoring box

    def test_two_cars_side_by_side_are_both_kept(self):
        left = (100, 100, 180, 180, 0.9, 2)
        right = (190, 100, 270, 180, 0.8, 2)  # adjacent, no overlap at all
        self.assertEqual(len(merge_fragments([left, right], ios_threshold=0.6)), 2)

    def test_two_cars_touching_slightly_are_both_kept(self):
        left = (100, 100, 180, 180, 0.9, 2)
        right = (170, 100, 250, 180, 0.8, 2)  # 10 px overlap on 80 px boxes: IoS 0.125
        merged = merge_fragments([left, right], ios_threshold=0.6)
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(box_ios(left, right), 10 * 80 / (80 * 80))

    def test_a_car_and_truck_box_on_one_vehicle_merge_to_the_better_one(self):
        car = (100, 100, 200, 200, 0.85, 2)
        truck = (98, 99, 202, 201, 0.55, 7)  # same vehicle, different class
        merged = merge_fragments([car, truck], ios_threshold=0.6)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][5], 2)  # the higher-scoring car keeps its class

    def test_zero_threshold_disables_the_merge(self):
        boxes = [(0, 0, 10, 10, 0.9, 2), (0, 0, 10, 10, 0.8, 2)]
        self.assertEqual(len(merge_fragments(boxes, ios_threshold=0)), 2)

    def test_ios_handles_zero_area_boxes(self):
        self.assertEqual(box_ios((5, 5, 5, 5), (0, 0, 10, 10)), 0.0)


class PolygonTests(unittest.TestCase):
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

    def test_points_inside_and_outside(self):
        self.assertTrue(point_in_polygon(5, 5, self.square))
        self.assertFalse(point_in_polygon(15, 5, self.square))
        self.assertFalse(point_in_polygon(-1, 5, self.square))

    def test_edge_counts_as_inside(self):
        self.assertTrue(point_in_polygon(0, 5, self.square))
        self.assertTrue(point_in_polygon(5, 10, self.square))

    def test_degenerate_polygon_is_not_a_lane(self):
        self.assertFalse(point_in_polygon(1, 1, [(0, 0), (2, 2)]))

    def test_polygon_scales_with_frame_size(self):
        scaled = scale_polygon([(0, 0), (10, 10)], (10, 10), (20, 40))
        self.assertEqual(scaled, [(0.0, 0.0), (20.0, 40.0)])
        with self.assertRaises(ValueError):
            scale_polygon([(0, 0)], (0, 10), (10, 10))


class DensityRowTests(unittest.TestCase):
    lanes = {
        "frame_size": [100, 100],
        "lanes": [
            {"id": 1, "polygon": [[0, 0], [50, 0], [50, 100], [0, 100]]},
            {"id": 2, "polygon": [[50, 0], [100, 0], [100, 100], [50, 100]]},
        ],
    }

    def test_counts_by_lane_and_class(self):
        detections = [
            (10, 10, 30, 40, 0.9, 2),  # car, bottom-centre (20, 40) in lane 1
            (5, 60, 25, 90, 0.8, 7),  # truck, bottom-centre (15, 90) in lane 1
            (60, 10, 80, 40, 0.7, 2),  # car, bottom-centre (70, 40) in lane 2
            (200, 200, 220, 220, 0.6, 2),  # outside both
        ]
        rows = density_rows(
            detections, self.lanes, (100, 100), camera_id="1043", captured_at="2026-10-29T14:03:00Z", image_name="a.jpg"
        )
        self.assertEqual([row["lane"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["vehicle_count"], 2)
        self.assertEqual(rows[0]["car"], 1)
        self.assertEqual(rows[0]["truck"], 1)
        self.assertEqual(rows[0]["bus"], 0)
        self.assertEqual(rows[1]["vehicle_count"], 1)
        self.assertEqual(rows[1]["car"], 1)

    def test_the_bottom_centre_decides_the_lane(self):
        # The box centre is in lane 1, but its bottom-centre sits in lane 2.
        detections = [(30, 10, 90, 60, 0.9, 2)]
        rows = density_rows(
            detections, self.lanes, (100, 100), camera_id="c", captured_at="t", image_name="a.jpg"
        )
        self.assertEqual(rows[0]["vehicle_count"], 0)
        self.assertEqual(rows[1]["vehicle_count"], 1)

    def test_lane_polygons_are_scaled_from_the_lanes_frame_size(self):
        # Lanes drawn on a 200x200 frame; the image is 100x100, so the polygons halve.
        lanes = {
            "frame_size": [200, 200],
            "lanes": [{"id": 1, "polygon": [[0, 0], [100, 0], [100, 200], [0, 200]]}],
        }
        detections = [(10, 10, 30, 90, 0.9, 2)]  # bottom-centre (20, 90) -> (10, 45) scaled: inside
        rows = density_rows(detections, lanes, (100, 100), camera_id="c", captured_at="t", image_name="a.jpg")
        self.assertEqual(rows[0]["vehicle_count"], 1)


class CsvTests(unittest.TestCase):
    def test_header_and_rows(self):
        rows = [
            {
                "captured_at": "2026-10-29T14:03:00Z",
                "camera_id": "1043",
                "image": "a.jpg",
                "lane": 1,
                "vehicle_count": 3,
                "car": 2,
                "truck": 1,
                "bus": 0,
                "motorcycle": 0,
                "bicycle": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "density.csv"
            write_csv(rows, path)
            with path.open() as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, CSV_COLUMNS)
                read = list(reader)
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0]["lane"], "1")
        self.assertEqual(read[0]["vehicle_count"], "3")


if __name__ == "__main__":
    unittest.main()
