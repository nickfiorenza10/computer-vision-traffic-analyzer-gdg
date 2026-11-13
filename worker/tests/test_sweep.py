"""Tests for the 30-minute camera sweep (T15): no network, no GPU, no database.

The sweeper takes its catalogue, its still fetcher, its detector and its store as arguments,
so a test can watch a whole sweep happen in milliseconds and assert exactly what was fetched,
counted and written.
"""

import hashlib
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import sweep  # noqa: E402


def png_bytes(value: int, size: int = 64) -> bytes:
    """Distinct still bytes, so the duplicate check has something to compare."""
    return (b"JPG" + str(value).encode() * (size // 3 + 1))[:size]


def still_image(seed: int = 0, brightness: int = 120) -> bytes:
    """A real JPEG, so the brightness and detection paths see a decoder result."""
    import cv2

    image = np.full((240, 320, 3), brightness, dtype=np.uint8)
    for index in range(4):
        x = 20 + (seed * 13 + index * 25) % 280
        cv2.rectangle(image, (x, 150), (x + 24, 200), (brightness - 60, brightness - 60, brightness - 60), -1)
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    return buffer.tobytes()


class FakeCamera:
    def __init__(self, camera_id: str, image_url: str = "", latitude=38.5, longitude=-121.5):
        self.camera_id = camera_id
        self.image_url = image_url
        self.location = f"somewhere on {camera_id}"
        self.nearby_place = "near place"
        self.county = "Sacramento"
        self.route = "I-80"
        self.direction = "Eastbound"
        self.latitude = latitude
        self.longitude = longitude
        self.stream_url = "https://example.test/stream.m3u8"


class FakeStore:
    def __init__(self, cameras, history=None):
        self.cameras = cameras
        self.history_map = history or {}
        self.sweeps = []
        self.upserted = []

    def upsert_cameras(self, cameras):
        self.upserted.extend(cameras)
        return len(cameras)

    def list_cameras(self, districts):
        return [
            {
                "camera_id": f"caltrans-{c.camera_id}",
                "image_url": c.image_url,
                "latitude": c.latitude,
                "longitude": c.longitude,
            }
            for c in self.cameras
        ]

    def history(self, camera_id, limit=60):
        return self.history_map.get(camera_id, [])

    def insert_sweep(self, row):
        self.sweeps.append(row)


class SweepTests(unittest.TestCase):
    def make(self, cameras, *, stills=3, counts=None, history=None, fail=()):
        store = FakeStore(cameras, history)
        calls = {"fetch": [], "sleep": []}
        counter = {"n": 0}

        def fetch(url):
            calls["fetch"].append(url)
            if url in fail:
                raise RuntimeError("HTTP 404")
            counter["n"] += 1
            return still_image(counter["n"])

        def detect(data):
            if counts is None:
                return 4
            return counts(calls["fetch"][-1] if calls["fetch"] else "")

        sweeper = sweep.Sweeper(
            store=store,
            fetch=fetch,
            detect=detect,
            districts=(3,),
            stills=stills,
            spacing_seconds=0.0,
            sleep=lambda seconds: calls["sleep"].append(seconds),
            now=lambda: datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc),
        )
        sweeper._catalog = lambda districts: cameras
        return sweeper, store, calls

    def test_one_row_per_camera_and_one_fetch_per_still(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg"), FakeCamera("D3-2", "https://e.test/2.jpg")]
        sweeper, store, calls = self.make(cameras)
        summary = sweeper.run_sweep()
        self.assertEqual(summary["cameras"], 2)
        self.assertEqual(summary["swept"], 2)
        self.assertEqual(len(calls["fetch"]), 6)  # 2 cameras x 3 stills
        self.assertEqual(len(store.sweeps), 2)
        row = store.sweeps[0]
        self.assertEqual(row["camera_id"], "caltrans-D3-1")
        self.assertEqual(row["frames"], 3)
        self.assertEqual(row["vehicles_mean"], 4.0)
        self.assertEqual(row["detector"], "yolo")
        self.assertIn(row["level"], ("learning", "light", "moderate", "heavy"))

    def test_stills_are_spaced_a_minute_apart(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        sweeper, _, calls = self.make(cameras, stills=4)
        sweeper.spacing_seconds = 60.0
        sweeper.run_sweep()
        self.assertEqual(calls["sleep"], [60.0, 60.0, 60.0])  # not after the last still

    def test_a_failed_camera_is_skipped_and_the_others_continue(self):
        cameras = [
            FakeCamera("D3-1", "https://e.test/dead.jpg"),
            FakeCamera("D3-2", "https://e.test/live.jpg"),
        ]
        sweeper, store, calls = self.make(cameras, fail={"https://e.test/dead.jpg"})
        summary = sweeper.run_sweep()
        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(store.sweeps), 1)
        self.assertEqual(store.sweeps[0]["camera_id"], "caltrans-D3-2")
        # The dead camera is not retried in a tight loop: one attempt per sweep.
        self.assertEqual(calls["fetch"].count("https://e.test/dead.jpg"), 1)

    def test_identical_stills_are_counted_once(self):
        cameras = [FakeCamera("D3-1", "https://e.test/same.jpg")]
        store = FakeStore(cameras)
        fetches = {"n": 0}

        def fetch(url):
            fetches["n"] += 1
            return still_image(1)  # the feed has not refreshed yet

        sweeper = sweep.Sweeper(
            store=store,
            fetch=fetch,
            detect=lambda data: 5,
            districts=(3,),
            stills=4,
            spacing_seconds=0.0,
            sleep=lambda seconds: None,
        )
        sweeper._catalog = lambda districts: cameras
        sweeper.run_sweep()
        self.assertEqual(len(store.sweeps), 1)
        self.assertEqual(store.sweeps[0]["frames"], 1)
        self.assertEqual(sweeper.status_dict()["skipped_duplicates"], 3)

    def test_the_level_comes_from_the_camera_s_own_history(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        busy_history = {"caltrans-D3-1": [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0]}
        sweeper, store, _ = self.make(cameras, counts=lambda url: 9, history=busy_history)
        sweeper.run_sweep()
        self.assertEqual(store.sweeps[-1]["level"], "heavy")

        quiet_history = {"caltrans-D3-1": [9.0, 9.0, 10.0, 10.0, 11.0, 11.0, 12.0, 12.0]}
        sweeper, store, _ = self.make(cameras, counts=lambda url: 1, history=quiet_history)
        sweeper.run_sweep()
        self.assertEqual(store.sweeps[-1]["level"], "light")

    def test_a_dark_camera_is_night_not_a_light_road(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        store = FakeStore(cameras)

        def fetch(url):
            return still_image(1, brightness=8)  # a black frame

        sweeper = sweep.Sweeper(
            store=store,
            fetch=fetch,
            detect=lambda data: 0,
            districts=(3,),
            stills=2,
            spacing_seconds=0.0,
            sleep=lambda seconds: None,
            now=lambda: datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc),  # 2 am PDT
        )
        sweeper._catalog = lambda districts: cameras
        sweeper.run_sweep()
        self.assertEqual(store.sweeps[-1]["level"], "night")

    def test_the_sun_decides_day_and_night(self):
        """Sacramento in PDT and Tampa in EDT: night is night however bright, a dark road in
        daylight is unknown (a camera problem), and in twilight brightness decides."""
        places = {
            "Sacramento": (38.58, -121.49, -7),
            "Tampa": (27.95, -82.46, -4),
        }
        cases = [
            # local time, road brightness, expected level
            ((2, 0), 120, "night"),
            ((13, 0), 30, "unknown"),
            ((13, 0), 120, "learning"),
        ]
        twilight = {"Sacramento": (18, 20), "Tampa": (19, 0)}  # between sunset and civil dusk
        for place, (lat, lon, offset) in places.items():
            zone = timezone(timedelta(hours=offset))
            place_cases = cases + [(twilight[place], 120, "learning"), (twilight[place], 30, "night")]
            for (hour, minute), bright, expected in place_cases:
                with self.subTest(place=place, at=f"{hour}:{minute:02d}", brightness=bright):
                    cameras = [FakeCamera("D3-1", "https://e.test/1.jpg", lat, lon)]
                    store = FakeStore(cameras)
                    frames = iter(range(100))
                    sweeper = sweep.Sweeper(
                        store=store,
                        fetch=lambda url: still_image(next(frames), brightness=bright),
                        detect=lambda data: 3,
                        districts=(3,),
                        stills=2,
                        spacing_seconds=0.0,
                        sleep=lambda seconds: None,
                        now=lambda: datetime(2026, 10, 29, hour, minute, tzinfo=zone),
                    )
                    sweeper._catalog = lambda districts: cameras
                    sweeper.run_sweep()
                    row = store.sweeps[-1]
                    self.assertEqual(row["level"], expected)
                    self.assertEqual(row["vehicles_mean"], 3.0)  # counts are kept either way
                    self.assertEqual(
                        sweeper.status.dark_in_daylight,
                        ["caltrans-D3-1"] if expected == "unknown" else [],
                    )

    def test_without_coordinates_brightness_decides(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg", None, None)]
        store = FakeStore(cameras)
        sweeper = sweep.Sweeper(
            store=store,
            fetch=lambda url: still_image(1, brightness=8),
            detect=lambda data: 2,
            districts=(3,),
            stills=1,
            spacing_seconds=0.0,
            sleep=lambda seconds: None,
            now=lambda: datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc),  # 1 pm PDT
        )
        sweeper._catalog = lambda districts: cameras
        sweeper.run_sweep()
        self.assertEqual(store.sweeps[-1]["level"], "night")

    def test_a_camera_with_no_image_url_is_not_fetched(self):
        cameras = [FakeCamera("D3-1", ""), FakeCamera("D3-2", "https://e.test/2.jpg")]
        sweeper, store, calls = self.make(cameras)
        summary = sweeper.run_sweep()
        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertNotIn("", calls["fetch"])

    def test_the_catalogue_is_refreshed_when_stale(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        sweeper, store, _ = self.make(cameras)
        sweeper.run_sweep()
        first = len(store.upserted)
        self.assertGreaterEqual(first, 1)
        # A second sweep inside the refresh window does not re-fetch the catalogue.
        sweeper.run_sweep()
        self.assertEqual(len(store.upserted), first)


class StatusTests(unittest.TestCase):
    def test_status_reports_what_happened(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        sweeper, store, _ = SweepTests.make(self, cameras)
        self.assertFalse(sweeper.status_dict()["running"])
        sweeper.run_sweep()
        status = sweeper.status_dict()
        self.assertEqual(status["cameras"], 1)
        self.assertEqual(status["swept"], 1)
        self.assertFalse(status["running"])
        self.assertEqual(status["districts"], [3])
        self.assertIsNotNone(status["last_started_at"])
        self.assertIsNotNone(status["last_finished_at"])

    def test_an_unknown_error_is_reported_not_raised(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        sweeper, store, _ = SweepTests.make(self, cameras)
        store.list_cameras = lambda districts: (_ for _ in ()).throw(RuntimeError("database is down"))
        summary = sweeper.run_sweep()
        self.assertIn("database is down", summary["error"])
        self.assertIn("database is down", sweeper.status_dict()["last_error"])
        self.assertFalse(sweeper.status_dict()["running"])


class SchedulerTests(unittest.TestCase):
    def test_a_sleeping_pc_gets_one_sweep_not_several(self):
        cameras = [FakeCamera("D3-1", "https://e.test/1.jpg")]
        runs = []
        sweeper, store, _ = SweepTests.make(self, cameras)

        def fake_sweep():
            runs.append(sweeper._now())
            return {"cameras": 1, "swept": 1, "failed": 0}

        sweeper.run_sweep = fake_sweep
        sweeper.interval_seconds = 1800
        sweeper.start()
        try:
            deadline = time.time() + 5
            while len(runs) < 1 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            sweeper.stop()
        self.assertGreaterEqual(len(runs), 1)
        # The next run is scheduled from now, not from the missed slot.
        next_run = datetime.fromisoformat(sweeper.status_dict()["next_run_at"])
        self.assertGreater(next_run - runs[0], timedelta(minutes=29))


class PostgresStoreTests(unittest.TestCase):
    def test_the_camera_id_is_namespaced_for_the_database(self):
        from worker import sweep_store

        self.assertIn("ON CONFLICT (camera_id) DO UPDATE", sweep_store.UPSERT_CAMERAS)
        self.assertIn("ON CONFLICT (camera_id, swept_at) DO NOTHING", sweep_store.INSERT_SWEEP)
        self.assertIn("district = ANY", sweep_store.LIST_CAMERAS)
        self.assertIn("ORDER BY swept_at DESC", sweep_store.HISTORY)

    def test_a_camera_becomes_a_row_with_the_database_s_own_id(self):
        from worker import sweep_store

        row = sweep_store.camera_row(FakeCamera("D3-41", "https://e.test/41.jpg"))
        self.assertEqual(row["camera_id"], "caltrans-D3-41")
        self.assertEqual(row["district"], 3)
        self.assertEqual(row["image_url"], "https://e.test/41.jpg")
        self.assertEqual(row["route"], "I-80")
        self.assertTrue(row["has_stream"])

    def test_a_camera_without_a_still_url_is_stored_as_null(self):
        from worker import sweep_store

        row = sweep_store.camera_row(FakeCamera("D3-1", ""))
        self.assertIsNone(row["image_url"])


if __name__ == "__main__":
    unittest.main()
