"""Tests for the camera view-change check.

The images are built here rather than taken from a video fixture: a scene with real
texture, a fixed overlay strip that imitates Caltrans' burned-in text, and transforms
applied with OpenCV, so each case is exact and the tests stay fast.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import view_check  # noqa: E402

WIDTH, HEIGHT = 1280, 720


def scene(seed: int = 7) -> np.ndarray:
    """A textured road-ish scene: enough structure for ORB to find real features."""
    rng = np.random.default_rng(seed)
    image = np.full((HEIGHT, WIDTH, 3), 70, dtype=np.uint8)
    # Noise gives every patch unique texture, so a shift is measurable and a rotation is
    # not confusable with noise.
    noise = rng.integers(-35, 35, (HEIGHT, WIDTH, 1), dtype=np.int16)
    image = np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    for _ in range(60):
        colour = tuple(int(value) for value in rng.integers(40, 230, 3))
        x1, y1 = int(rng.integers(0, WIDTH - 60)), int(rng.integers(0, HEIGHT - 60))
        x2, y2 = x1 + int(rng.integers(20, 140)), y1 + int(rng.integers(10, 80))
        cv2.rectangle(image, (x1, y1), (min(x2, WIDTH - 1), min(y2, HEIGHT - 1)), colour, -1)
    for _ in range(40):
        colour = tuple(int(value) for value in rng.integers(120, 255, 3))
        start = (int(rng.integers(0, WIDTH)), int(rng.integers(0, HEIGHT)))
        end = (int(rng.integers(0, WIDTH)), int(rng.integers(0, HEIGHT)))
        cv2.line(image, start, end, colour, int(rng.integers(1, 4)))
    return image


def with_overlay(image: np.ndarray, label: str = "I-5 SB AT MM 12.3", text_dx: int = 0) -> np.ndarray:
    """Burn in a top name plate and a bottom timestamp, like the Caltrans feeds do."""
    burned = image.copy()
    cv2.rectangle(burned, (0, 0), (WIDTH, 46), (0, 0, 0), -1)
    cv2.putText(burned, label, (18 + text_dx, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.rectangle(burned, (0, HEIGHT - 40), (WIDTH, HEIGHT), (0, 0, 0), -1)
    cv2.putText(burned, "10/29/2026 14:07:33", (18, HEIGHT - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return burned


def shifted(image: np.ndarray, dx_fraction: float, dy_fraction: float = 0.0) -> np.ndarray:
    matrix = np.float32([[1, 0, WIDTH * dx_fraction], [0, 1, HEIGHT * dy_fraction]])
    return cv2.warpAffine(image, matrix, (WIDTH, HEIGHT), borderMode=cv2.BORDER_REPLICATE)


def zoomed(image: np.ndarray, factor: float) -> np.ndarray:
    matrix = cv2.getRotationMatrix2D((WIDTH / 2, HEIGHT / 2), 0, factor)
    return cv2.warpAffine(image, matrix, (WIDTH, HEIGHT), borderMode=cv2.BORDER_REPLICATE)


def rotated(image: np.ndarray, degrees: float) -> np.ndarray:
    matrix = cv2.getRotationMatrix2D((WIDTH / 2, HEIGHT / 2), degrees, 1.0)
    return cv2.warpAffine(image, matrix, (WIDTH, HEIGHT), borderMode=cv2.BORDER_REPLICATE)


class SameTests(unittest.TestCase):
    def setUp(self):
        self.reference = with_overlay(scene())

    def test_the_same_frame_is_same(self):
        verdict = view_check.compare_frames(self.reference, self.reference.copy())
        self.assertEqual(verdict.verdict, view_check.SAME, verdict.describe())

    def test_noise_brightness_and_blur_alone_are_same(self):
        rng = np.random.default_rng(3)
        noisy = np.clip(
            self.reference.astype(np.int16) + rng.integers(-12, 12, self.reference.shape, dtype=np.int16),
            0,
            255,
        ).astype(np.uint8)
        brighter = cv2.convertScaleAbs(self.reference, alpha=1.0, beta=25)
        blurred = cv2.GaussianBlur(self.reference, (5, 5), 0)
        for name, candidate in (("noise", noisy), ("brighter", brighter), ("blur", blurred)):
            with self.subTest(name):
                verdict = view_check.compare_frames(self.reference, candidate)
                self.assertEqual(verdict.verdict, view_check.SAME, verdict.describe())

    def test_a_small_shift_inside_the_limit_is_still_same(self):
        # One percent is below the 3% default: a wobble in the mount, not a moved camera.
        verdict = view_check.compare_frames(self.reference, shifted(self.reference, 0.01))
        self.assertEqual(verdict.verdict, view_check.SAME, verdict.describe())


class MovedTests(unittest.TestCase):
    def setUp(self):
        self.reference = with_overlay(scene())

    def test_a_five_percent_shift_is_moved(self):
        verdict = view_check.compare_frames(self.reference, shifted(self.reference, 0.05))
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())
        self.assertGreater(verdict.shift_fraction, 0.04)
        self.assertGreater(abs(verdict.shift[0]), 20)  # 5% of the 640 px analysis width

    def test_a_fifteen_percent_shift_is_moved(self):
        verdict = view_check.compare_frames(self.reference, shifted(self.reference, 0.15))
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())
        self.assertGreater(verdict.shift_fraction, 0.10)

    def test_a_ten_percent_zoom_is_moved(self):
        verdict = view_check.compare_frames(self.reference, zoomed(self.reference, 1.10))
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())
        self.assertGreater(abs(verdict.scale - 1.0), 0.05)

    def test_a_five_degree_rotation_is_moved(self):
        verdict = view_check.compare_frames(self.reference, rotated(self.reference, 5.0))
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())
        self.assertGreater(abs(verdict.rotation_deg), 2.0)

    def test_an_identical_overlay_does_not_hide_a_moved_view(self):
        # The trap: the text stays put while the road moves, so a matcher that looks at
        # the text says "same". The frame is the shifted scene with the overlay redrawn
        # in exactly its old place.
        moved_scene = with_overlay(shifted(scene(), 0.12))
        verdict = view_check.compare_frames(self.reference, moved_scene)
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())

    def test_a_changed_overlay_is_ignored_because_it_is_masked(self):
        # The road is identical; only the burned-in text has moved along the strip. The
        # check must not read that as a moved camera, which is what the mask is for.
        road = scene()
        reference = with_overlay(road, label="I-5 SB AT MM 12.3")
        candidate = with_overlay(road, label="I-5 SB AT MM 12.3", text_dx=140)
        verdict = view_check.compare_frames(reference, candidate)
        self.assertEqual(verdict.verdict, view_check.SAME, verdict.describe())

    def test_a_moved_road_is_still_moved_when_the_overlay_matches(self):
        # The same masking must not blind the check to the road itself.
        reference = with_overlay(scene(), label="I-5 SB AT MM 12.3")
        candidate = with_overlay(shifted(scene(), 0.12), label="I-5 SB AT MM 12.3")
        verdict = view_check.compare_frames(reference, candidate)
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())


class UnknownTests(unittest.TestCase):
    def test_a_dark_low_texture_frame_is_unknown_not_a_guess(self):
        reference = with_overlay(scene())
        night = (scene(seed=11).astype(np.float32) * 0.05).astype(np.uint8)
        night = with_overlay(night)
        verdict = view_check.compare_frames(reference, night)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())
        self.assertIn("detail", verdict.reason)

    def test_a_blank_frame_is_unknown(self):
        blank = np.full((HEIGHT, WIDTH, 3), 128, dtype=np.uint8)
        verdict = view_check.compare_frames(with_overlay(scene()), blank)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())

    def test_unrelated_images_are_unknown_rather_than_moved(self):
        # Two different scenes are not evidence that the camera moved, and saying so
        # would be a wrong answer rather than an honest one.
        verdict = view_check.compare_frames(with_overlay(scene(seed=1)), with_overlay(scene(seed=99)))
        self.assertIn(verdict.verdict, (view_check.UNKNOWN, view_check.MOVED), verdict.describe())
        if verdict.verdict == view_check.UNKNOWN:
            self.assertTrue(verdict.reason)

    def test_no_frame_is_unknown(self):
        self.assertEqual(view_check.compare_frames(None, scene()).verdict, view_check.UNKNOWN)
        self.assertEqual(view_check.compare_frames(scene(), None).verdict, view_check.UNKNOWN)


class BackgroundTests(unittest.TestCase):
    def test_the_median_drops_what_moves(self):
        frames = []
        for index in range(9):
            frame = np.full((60, 80), 20, dtype=np.uint8)
            cv2.circle(frame, (5 + index * 8, 30), 6, 255, -1)  # a bright thing crossing
            frames.append(frame)
        background = view_check.median_background(frames)
        self.assertEqual(background.shape, (60, 80))
        self.assertLess(int(background.max()), 80)  # the bright thing is gone
        self.assertEqual(int(background[30, 45]), 20)  # the road is still there

    def test_one_frame_is_its_own_background(self):
        frame = np.full((30, 40, 3), 17, dtype=np.uint8)
        background = view_check.median_background([frame])
        self.assertEqual(background.shape, (30, 40))
        self.assertEqual(int(background.mean()), 17)

    def test_no_frames_is_none(self):
        self.assertIsNone(view_check.median_background([]))
        self.assertIsNone(view_check.median_background(None))

    def test_frames_of_different_sizes_are_refused(self):
        with self.assertRaises(ValueError):
            view_check.median_background(
                [np.zeros((10, 10), np.uint8), np.zeros((12, 12), np.uint8)]
            )


class DarkSceneTests(unittest.TestCase):
    """The review's finding: a dark scene's only matchable detail is moving headlights."""

    @staticmethod
    def dark_frames(count: int = 30):
        base = np.full((HEIGHT, WIDTH, 3), 22, dtype=np.uint8)  # near-black road
        frames = []
        for index in range(count):
            frame = base.copy()
            x = (80 + index * 60) % (WIDTH - 80)
            cv2.circle(frame, (x, 520), 22, (255, 255, 255), -1)
            cv2.circle(frame, (x + 60, 520), 22, (245, 245, 245), -1)
            cv2.circle(frame, (x + 300, 400), 18, (230, 230, 230), -1)
            frames.append(frame)
        return frames

    def test_single_frames_of_a_still_dark_camera_would_say_moved(self):
        # This is the old bug, kept as a test so the fix cannot regress into it: RANSAC
        # fits the headlights and calls the still camera moved.
        frames = self.dark_frames()
        verdict = view_check.compare_frames(frames[0], frames[15])
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())

    def test_median_backgrounds_of_the_same_frames_never_say_moved(self):
        frames = self.dark_frames()
        reference = view_check.median_background(frames[:15])
        candidate = view_check.median_background(frames[15:])
        self.assertEqual(int(reference.max()), 22)  # every headlight is gone
        verdict = view_check.compare_frames(reference, candidate)
        self.assertNotEqual(verdict.verdict, view_check.MOVED, verdict.describe())

    def test_a_dark_clip_is_checked_with_backgrounds_end_to_end(self):
        directory = Path(tempfile.mkdtemp())
        frames = self.dark_frames()
        lanes = directory / "lanes.json"
        reference = view_check.save_reference(frames[:15], lanes)
        lanes.write_text(json.dumps({"camera": "D3-52", "reference_image": reference}))
        video = directory / "clip.mp4"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (WIDTH, HEIGHT))
        for frame in frames:
            writer.write(frame)
        writer.release()
        verdict = view_check.check_video(lanes, video)
        self.assertNotEqual(verdict.verdict, view_check.MOVED, verdict.describe())


class PlausibleTransformTests(unittest.TestCase):
    def test_a_two_times_zoom_is_unknown_by_default(self):
        reference = with_overlay(scene())
        verdict = view_check.compare_frames(reference, zoomed(reference, 2.0))
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())
        self.assertIn("implausible", verdict.reason)

    def test_the_same_zoom_is_moved_once_the_bound_allows_it(self):
        # Proves the bound is what produced unknown, rather than a lucky failure to match.
        reference = with_overlay(scene())
        verdict = view_check.compare_frames(
            reference, zoomed(reference, 2.0), min_plausible_scale=0.4, max_plausible_scale=2.5
        )
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())

    def test_a_rotation_past_fifteen_degrees_is_unknown_by_default(self):
        reference = with_overlay(scene())
        verdict = view_check.compare_frames(reference, rotated(reference, 20.0))
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())
        self.assertIn("implausible", verdict.reason)


class MaskTests(unittest.TestCase):
    def test_the_default_mask_removes_the_top_and_bottom(self):
        mask = view_check.mask_for((100, 50))
        self.assertEqual(int(mask[0, 0]), 0)  # top 25%
        self.assertEqual(int(mask[24, 0]), 0)
        self.assertEqual(int(mask[25, 0]), 255)
        self.assertEqual(int(mask[95, 0]), 0)  # bottom 10%
        self.assertEqual(int(mask[89, 0]), 255)

    def test_a_per_camera_mask_is_honoured(self):
        mask = view_check.mask_for((100, 50), top=0.4, bottom=0.2)
        self.assertEqual(int(mask[39, 0]), 0)
        self.assertEqual(int(mask[40, 0]), 255)
        self.assertEqual(int(mask[81, 0]), 0)


class ReferenceFileTests(unittest.TestCase):
    def test_the_reference_is_grayscale_and_at_most_640_wide(self):
        lanes = Path(tempfile.mkdtemp()) / "lanes_caltrans_D3-52.json"
        frame = scene()  # 1280 wide
        name = view_check.save_reference(frame, lanes)
        self.assertEqual(name, "lanes_caltrans_D3-52.reference.jpg")
        saved = cv2.imread(str(lanes.with_name(name)), cv2.IMREAD_UNCHANGED)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.shape[1], 640)
        self.assertEqual(saved.ndim, 2)  # grayscale
        self.assertLess(lanes.with_name(name).stat().st_size, 200 * 1024)

    def test_the_name_helper_matches_what_is_written(self):
        lanes = Path("pipeline/lanes_caltrans_D3-1.json")
        self.assertEqual(view_check.reference_name_for(lanes), "lanes_caltrans_D3-1.reference.jpg")


class VideoTests(unittest.TestCase):
    FPS = 10.0

    def _write_video(self, frames, path: Path, fps: float | None = None):
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps or self.FPS, (WIDTH, HEIGHT)
        )
        for frame in frames:
            writer.write(frame)
        writer.release()

    def _lanes(self, directory: Path, reference, name: str = "lanes_caltrans_D3-52.json") -> Path:
        lanes = directory / name
        saved = view_check.save_reference(reference, lanes)
        lanes.write_text(
            json.dumps(
                {"camera": "D3-52", "reference_image": saved, "frame_size": [WIDTH, HEIGHT], "lanes": []}
            )
        )
        return lanes

    def test_a_short_clip_reads_its_background_and_can_say_same(self):
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = self._lanes(directory, reference)
        video = directory / "clip.mp4"
        self._write_video([reference] * 10, video)

        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.SAME, verdict.describe())
        self.assertEqual(verdict.reference, "lanes_caltrans_D3-52.reference.jpg")

    def test_a_short_clip_can_never_say_moved(self):
        # One sample cannot corroborate a move, and a false "moved" is what blocked night
        # counting, so a clip too short for a second sample is honest and says unknown.
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = self._lanes(directory, reference)
        video = directory / "clip.mp4"
        self._write_video([shifted(reference, 0.15)] * 10, video)

        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())
        self.assertIn("too short", verdict.reason)

    def test_samples_that_agree_report_moved(self):
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = self._lanes(directory, reference)
        video = directory / "clip.mp4"
        moved_frame = shifted(reference, 0.15)
        self._write_video([moved_frame] * 260, video)  # 26 s at 10 fps: three samples fit

        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.MOVED, verdict.describe())
        self.assertIn("3 samples", verdict.reason)

    def test_samples_that_disagree_are_unknown(self):
        # The camera stays put for the first 15 s and is panned after: the samples cannot
        # agree, and neither answer is reported.
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = self._lanes(directory, reference)
        video = directory / "clip.mp4"
        still = [reference] * 150
        panned = [shifted(reference, 0.15)] * 110
        self._write_video(still + panned, video)

        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN, verdict.describe())
        self.assertIn("disagree", verdict.reason)

    def test_video_without_a_reference_image_is_unknown(self):
        directory = Path(tempfile.mkdtemp())
        lanes = directory / "lanes.json"
        lanes.write_text(json.dumps({"camera": "x", "lanes": []}))
        video = directory / "clip.mp4"
        self._write_video([scene()], video)
        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN)
        self.assertIn("no reference image", verdict.reason)

    def test_the_lanes_file_s_own_view_mask_is_used(self):
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = directory / "lanes.json"
        name = view_check.save_reference(reference, lanes)
        lanes.write_text(
            json.dumps(
                {
                    "camera": "x",
                    "reference_image": name,
                    "view_mask": {"top": 0.35, "bottom": 0.02},
                }
            )
        )
        video = directory / "clip.mp4"
        self._write_video([reference], video)
        captured = {}
        original = view_check.compare_frames

        def spy(ref, frame, **kwargs):
            captured.update(kwargs)
            return original(ref, frame, **kwargs)

        view_check.compare_frames = spy
        try:
            view_check.check_video(lanes, video, at_seconds=0.0)
        finally:
            view_check.compare_frames = original
        self.assertEqual(captured["mask_top"], 0.35)
        self.assertEqual(captured["mask_bottom"], 0.02)

    def test_an_explicit_mask_flag_beats_the_lanes_file(self):
        directory = Path(tempfile.mkdtemp())
        reference = with_overlay(scene())
        lanes = directory / "lanes.json"
        name = view_check.save_reference(reference, lanes)
        lanes.write_text(
            json.dumps({"reference_image": name, "view_mask": {"top": 0.35, "bottom": 0.02}})
        )
        video = directory / "clip.mp4"
        self._write_video([reference], video)
        captured = {}
        original = view_check.compare_frames

        def spy(ref, frame, **kwargs):
            captured.update(kwargs)
            return original(ref, frame, **kwargs)

        view_check.compare_frames = spy
        try:
            view_check.check_video(lanes, video, at_seconds=0.0, mask_top=0.1)
        finally:
            view_check.compare_frames = original
        self.assertEqual(captured["mask_top"], 0.1)
        self.assertEqual(captured["mask_bottom"], 0.02)

    def test_a_missing_reference_file_is_unknown_not_same(self):
        directory = Path(tempfile.mkdtemp())
        lanes = directory / "lanes.json"
        lanes.write_text(json.dumps({"camera": "x", "reference_image": "gone.reference.jpg"}))
        video = directory / "clip.mp4"
        self._write_video([scene()], video)
        verdict = view_check.check_video(lanes, video)
        self.assertEqual(verdict.verdict, view_check.UNKNOWN)
        self.assertIn("missing", verdict.reason)


if __name__ == "__main__":
    unittest.main()
