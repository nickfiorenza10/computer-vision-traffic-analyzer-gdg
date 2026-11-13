import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compare_counts import compare, match_lane  # noqa: E402
from hand_count import KEY_SPACE, HandCounter, load_existing, save  # noqa: E402


class HandCounterKeyTests(unittest.TestCase):
    def counter(self, **kwargs):
        return HandCounter(lane_ids=[1, 2, 3], start_frame=10, end_frame=100, **kwargs)

    def test_lane_keys_count_at_current_frame(self):
        c = self.counter()
        c.seek(42)
        c.handle_key(ord("2"))
        c.handle_key(ord("2"))
        c.handle_key(ord("3"))
        self.assertEqual(c.counts, [(42, 2), (42, 2), (42, 3)])
        self.assertEqual(c.tally(), {1: 0, 2: 2, 3: 1})

    def test_unknown_lane_key_is_ignored(self):
        c = self.counter()
        c.handle_key(ord("7"))
        self.assertEqual(c.counts, [])

    def test_undo_removes_last_count(self):
        c = self.counter()
        c.handle_key(ord("1"))
        c.handle_key(ord("2"))
        c.handle_key(ord("u"))
        self.assertEqual(c.counts, [(10, 1)])
        c.handle_key(ord("u"))
        c.handle_key(ord("u"))  # nothing left; no error
        self.assertEqual(c.counts, [])

    def test_stepping_pauses_and_stays_in_range(self):
        c = self.counter()
        c.handle_key(KEY_SPACE)
        self.assertTrue(c.playing)
        c.handle_key(ord("d"))
        self.assertFalse(c.playing)
        self.assertEqual(c.frame, 11)
        c.handle_key(ord("a"))
        c.handle_key(ord("a"))
        self.assertEqual(c.frame, 10)  # can't go before start_frame
        c.seek(500)
        self.assertEqual(c.frame, 100)
        self.assertEqual(c.furthest_frame, 100)

    def test_speed_is_clamped(self):
        c = self.counter()
        for _ in range(10):
            c.handle_key(ord("]"))
        self.assertEqual(c.speed, 2.0)
        for _ in range(10):
            c.handle_key(ord("["))
        self.assertEqual(c.speed, 0.25)

    def test_quit_and_save_actions(self):
        c = self.counter()
        self.assertEqual(c.handle_key(ord("q")), "quit")
        self.assertEqual(c.handle_key(27), "quit")
        self.assertEqual(c.handle_key(ord("s")), "save")

    def test_save_and_resume_round_trip(self):
        c = self.counter()
        c.seek(30)
        c.handle_key(ord("1"))
        c.seek(55)
        c.handle_key(ord("3"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip_hand.csv"
            save(c, out, Path("clip.mp4"), Path("lanes.json"), 25.0)
            counts, meta = load_existing(out)
            self.assertEqual(counts, [(30, 1), (55, 3)])
            self.assertEqual(meta["start_frame"], 10)
            self.assertEqual(meta["end_frame"], 55)
            self.assertEqual(meta["fps"], 25.0)
            self.assertIn("seconds", out.read_text().splitlines()[0])
            self.assertEqual(json.loads(out.with_suffix(".json").read_text())["counts"], 2)


class CompareTests(unittest.TestCase):
    def test_match_lane_within_tolerance(self):
        # hand at 100, 200, 300; pipeline 2 frames late on two, one extra, one missed
        matched, missed, extra = match_lane([100, 200, 300], [102, 202, 250], tolerance_frames=15)
        self.assertEqual((matched, missed, extra), (2, 1, 1))

    def test_each_event_matches_once(self):
        # two hand counts close together, one pipeline event: one match, one missed
        self.assertEqual(match_lane([100, 105], [103], tolerance_frames=15), (1, 1, 0))
        # one hand count, a double count by the pipeline: one match, one extra
        self.assertEqual(match_lane([100], [101, 104], tolerance_frames=15), (1, 0, 1))

    def test_compare_window_lanes_and_errors(self):
        hand = [(10, 1), (40, 1), (70, 2), (500, 1)]  # 500 is outside the window
        auto = [(12, 1), (41, 1), (45, 1), (900, 2)]  # 45 is a double count; 900 outside
        rows, bin_mae = compare(hand, auto, start_frame=0, end_frame=100, fps=30,
                                tolerance_seconds=0.5, bin_seconds=10)
        by_lane = {r["lane"]: r for r in rows}
        self.assertEqual(by_lane[1], {**by_lane[1], "hand": 2, "pipeline": 3, "matched": 2, "missed": 0, "extra": 1})
        self.assertEqual(by_lane[2], {**by_lane[2], "hand": 1, "pipeline": 0, "matched": 0, "missed": 1, "extra": 0})
        self.assertEqual(by_lane["all"]["count_error_pct"], 0.0)  # 3 vs 3: offsetting errors
        self.assertEqual(by_lane["all"]["recall_pct"], 66.7)
        self.assertEqual(by_lane["all"]["precision_pct"], 66.7)
        self.assertEqual(by_lane[2]["precision_pct"], None)
        # One 10 s bin (frames 0-100 at 30 fps); lane 1 off by 1, lane 2 off by 1
        self.assertEqual(bin_mae, 1.0)


    def test_only_lanes_filter(self):
        hand = [(10, 1)]
        auto = [(11, 1), (20, 2)]
        rows, _ = compare(hand, auto, 0, 100, 30, 0.5, 10, only_lanes={1})
        self.assertEqual([r["lane"] for r in rows], [1, "all"])
        self.assertEqual(rows[-1]["extra"], 0)

if __name__ == "__main__":
    unittest.main()
