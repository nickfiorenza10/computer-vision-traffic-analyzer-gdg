import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headlights import HEADLIGHT_CLASS_ID, HeadlightDetector, make_tracker, track  # noqa: E402

W, H = 640, 480


def dark_frame(noise_seed=None):
    """A dark road at night, with a little sensor noise."""
    rng = np.random.default_rng(noise_seed)
    return rng.integers(10, 40, size=(H, W, 3), dtype=np.uint8)


def draw_light(frame, cx, cy, radius=4, value=255):
    yy, xx = np.ogrid[:H, :W]
    frame[(yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2] = value


def warmed_detector(**kwargs):
    """A detector that has learned an empty dark scene, so it starts reporting."""
    detector = HeadlightDetector(warmup_frames=10, **kwargs)
    for i in range(40):
        detector.detect(dark_frame(i))
    return detector


class HeadlightDetectorTests(unittest.TestCase):
    def test_nothing_reported_during_warmup(self):
        detector = HeadlightDetector(warmup_frames=10)
        frame = dark_frame(0)
        draw_light(frame, 300, 300)
        for _ in range(10):
            self.assertEqual(len(detector.detect(frame)), 0)

    def test_moving_headlight_pair_is_one_vehicle(self):
        detector = warmed_detector()
        frame = dark_frame(99)
        draw_light(frame, 300, 300)
        draw_light(frame, 314, 300)  # a car's two headlights, side by side
        boxes = detector.detect(frame)
        self.assertEqual(len(boxes), 1)
        x1, y1, x2, y2, score, class_id = boxes[0]
        # Spans both lights, bottom edge at the lights, grown upward to vehicle proportions
        self.assertLessEqual(x1, 300)
        self.assertGreaterEqual(x2, 314)
        self.assertAlmostEqual(y2, 305, delta=3)
        self.assertGreaterEqual(y2 - y1, 0.7 * (x2 - x1))
        self.assertEqual(int(class_id), HEADLIGHT_CLASS_ID)
        self.assertGreater(score, 0.5)

    def test_two_cars_far_apart_stay_separate(self):
        detector = warmed_detector()
        frame = dark_frame(98)
        draw_light(frame, 150, 300)
        draw_light(frame, 450, 300)
        self.assertEqual(len(detector.detect(frame)), 2)

    def test_static_light_is_ignored(self):
        detector = HeadlightDetector(warmup_frames=10)
        for i in range(120):
            frame = dark_frame(i)
            draw_light(frame, 500, 60, radius=5)  # a street light: bright but never moves
            boxes = detector.detect(frame)
        self.assertEqual(len(boxes), 0)

    def test_dim_or_tiny_blobs_are_ignored(self):
        detector = warmed_detector()
        frame = dark_frame(97)
        draw_light(frame, 200, 300, radius=4, value=150)  # not bright enough to be a headlight core
        draw_light(frame, 400, 300, radius=1)  # bright but a single speck
        self.assertEqual(len(detector.detect(frame)), 0)

    def test_sizes_scale_with_frame_width(self):
        detector = HeadlightDetector(warmup_frames=0)
        self.assertEqual(detector.min_area, 12.0)  # defined for 640 px; scaled inside detect()
        wide = np.zeros((480, 1280, 3), dtype=np.uint8)
        for _ in range(20):
            detector.detect(wide)
        frame = wide.copy()
        # A 4 px blob is big enough at 640 px wide but too small at 1280 px (4x the area threshold).
        frame[300:302, 600:602] = 255
        self.assertEqual(len(detector.detect(frame)), 0)


class HeadlightTrackingTests(unittest.TestCase):
    def test_a_moving_car_keeps_one_track_id(self):
        detector = HeadlightDetector(warmup_frames=10)
        tracker = make_tracker()
        for i in range(40):
            detector.detect(dark_frame(i))
        ids = []
        for step in range(30):  # the car drives down the frame, 5 px per frame
            frame = dark_frame(100 + step)
            y = 150 + 5 * step
            draw_light(frame, 300, y)
            draw_light(frame, 314, y)
            tracked = track(tracker, detector.detect(frame), frame.shape)
            ids.extend(int(t) for t in tracked[:, 4])
        self.assertGreater(len(ids), 20)
        self.assertEqual(len(set(ids)), 1)

    def test_no_detections_gives_empty_array(self):
        tracker = make_tracker()
        tracked = track(tracker, np.zeros((0, 6), dtype=np.float32), (H, W, 3))
        self.assertEqual(tracked.shape, (0, 7))


if __name__ == "__main__":
    unittest.main()
