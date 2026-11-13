"""Tests for the shared traffic-level definition, used by the sweep and by live analysis."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from pipeline import traffic_level as tl  # noqa: E402


class LiveLevelTests(unittest.TestCase):
    def test_congestion_is_heavy_from_density_alone(self):
        self.assertEqual(tl.live_level(flow_per_min=5, density=9.0, speed=2.0), tl.HEAVY)

    def test_medium_density_crawling_is_heavy(self):
        self.assertEqual(tl.live_level(flow_per_min=5, density=4.5, speed=0.2), tl.HEAVY)

    def test_medium_density_moving_is_only_moderate(self):
        self.assertEqual(tl.live_level(flow_per_min=5, density=4.5, speed=1.5), tl.MODERATE)

    def test_steady_flow_is_moderate_even_when_thin(self):
        self.assertEqual(tl.live_level(flow_per_min=25, density=1.0, speed=2.0), tl.MODERATE)

    def test_an_empty_road_is_light(self):
        self.assertEqual(tl.live_level(flow_per_min=0, density=0.0, speed=0.0), tl.LIGHT)

    def test_the_thresholds_are_the_documented_constants(self):
        self.assertEqual(tl.live_level(0, tl.LIVE_HEAVY_DENSITY, 2.0), tl.HEAVY)
        self.assertEqual(tl.live_level(0, tl.LIVE_HEAVY_DENSITY - 0.1, 2.0), tl.MODERATE)
        self.assertEqual(tl.live_level(tl.LIVE_STEADY_FLOW, 0.0, 2.0), tl.MODERATE)

    def test_the_reason_says_which_measurement_decided(self):
        self.assertIn("vehicles", tl.live_reason(0, 9.5, 2.0))
        self.assertIn("slow", tl.live_reason(0, 4.0, 0.1))
        self.assertIn("light", tl.live_reason(0, 0.0, 2.0))


class SweepLevelTests(unittest.TestCase):
    def setUp(self):
        self.history = [1.0, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]  # ten past sweeps

    def test_a_camera_with_too_few_sweeps_is_learning(self):
        self.assertEqual(tl.sweep_level(50.0, [1.0, 2.0]), tl.LEARNING)
        self.assertEqual(tl.sweep_level(50.0, []), tl.LEARNING)

    def test_a_busy_sweep_is_heavy(self):
        self.assertEqual(tl.sweep_level(9.0, self.history), tl.HEAVY)

    def test_a_middling_sweep_is_moderate(self):
        self.assertEqual(tl.sweep_level(5.0, self.history), tl.MODERATE)

    def test_an_empty_sweep_is_light(self):
        self.assertEqual(tl.sweep_level(0.5, self.history), tl.LIGHT)

    def test_exactly_eight_sweeps_is_enough(self):
        self.assertEqual(tl.sweep_level(9.0, self.history[:8]), tl.HEAVY)


class LightingTests(unittest.TestCase):
    """The sun decides day and night; brightness only in twilight or without coordinates."""

    def test_night_is_night_however_bright_the_road(self):
        self.assertEqual(tl.lighting("night", 180.0), tl.NIGHT)
        self.assertEqual(tl.lighting("night", 10.0), tl.NIGHT)

    def test_a_dark_road_in_daylight_is_a_camera_problem(self):
        self.assertEqual(tl.lighting("day", 30.0), tl.DARK_IN_DAYLIGHT)
        self.assertEqual(tl.lighting("day", 120.0), tl.DAY)

    def test_twilight_and_unknown_place_fall_back_to_brightness(self):
        for phase in ("twilight", None):
            self.assertEqual(tl.lighting(phase, tl.NIGHT_MEAN_BRIGHTNESS - 0.1), tl.NIGHT)
            self.assertEqual(tl.lighting(phase, tl.NIGHT_MEAN_BRIGHTNESS), tl.DAY)


class NightTests(unittest.TestCase):
    def test_a_dark_frame_is_night(self):
        dark = np.full((10, 10, 3), 10, dtype=np.uint8)
        self.assertTrue(tl.is_night(tl.frame_brightness(dark)))

    def test_a_daytime_frame_is_not(self):
        bright = np.full((10, 10, 3), 120, dtype=np.uint8)
        self.assertFalse(tl.is_night(tl.frame_brightness(bright)))

    def test_the_threshold_is_a_documented_constant(self):
        self.assertTrue(tl.is_night(tl.NIGHT_MEAN_BRIGHTNESS - 0.1))
        self.assertFalse(tl.is_night(tl.NIGHT_MEAN_BRIGHTNESS))

    def test_headlights_do_not_make_a_dark_road_look_lit(self):
        # A dark road with a few bright headlights: the mean is dragged up, the median is
        # not. This is Caltrans D3-52, where the mean says 55 and the road is dark.
        frame = np.full((480, 640, 3), 20, dtype=np.uint8)
        frame[200:240, :] = 255  # glare and headlights across the middle of the road
        self.assertGreater(float(frame.mean()), tl.frame_brightness(frame) + 10)
        self.assertLess(tl.frame_brightness(frame), 40)
        self.assertTrue(tl.is_night(tl.frame_brightness(frame)))

    def test_the_overlay_strip_is_not_part_of_the_road(self):
        # A bright burned-in name plate must not make a dark road look lit either.
        frame = np.full((480, 640, 3), 18, dtype=np.uint8)
        frame[0:120, :] = 250  # the top 25%: exactly the masked band
        self.assertLess(tl.frame_brightness(frame), 40)


if __name__ == "__main__":
    unittest.main()
