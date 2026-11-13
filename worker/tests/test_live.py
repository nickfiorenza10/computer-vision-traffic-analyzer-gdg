"""Tests for live analysis (T16), without a GPU, a network or a real camera.

A fake capture hands over synthetic frames and a fake detector returns boxes for them, so
the session's timing, its 10-second windows, its stop rules and its HTTP surface are all
exercised on a laptop in about a second.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import supervision as sv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from worker import live, sweep  # noqa: E402
from worker.app import Settings, Worker, create_app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
WIDTH, HEIGHT = 320, 240


class FakeCapture:
    """A capture that produces frames on demand, like a stream that never ends."""

    def __init__(self, frames: int | None = None):
        self.reads = 0
        self.released = False
        self._frames = frames
        self._lock = threading.Lock()

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV's spelling
        return True

    def get(self, prop: int) -> float:  # noqa: N802
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(WIDTH)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(HEIGHT)
        return 0.0

    def read(self):
        with self._lock:
            if self._frames is not None and self.reads >= self._frames:
                return False, None
            self.reads += 1
            index = self.reads
        frame = np.full((HEIGHT, WIDTH, 3), 90, dtype=np.uint8)
        x = 20 + (index * 7) % (WIDTH - 60)
        cv2.rectangle(frame, (x, 150), (x + 40, 200), (240, 240, 240), -1)
        cv2.putText(frame, "D3-41", (6, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        return True, frame

    def release(self) -> None:
        self.released = True


def boxes_detector(count: int = 2):
    """A detector that reports `count` boxes moving across the frame, as (N, 6) boxes."""

    state = {"n": 0}

    def detect(frame):
        state["n"] += 1
        boxes = []
        for index in range(count):
            x = 10 + (state["n"] * 5 + index * 34) % (WIDTH - 50)
            boxes.append([x, 150, x + 30, 200, 0.9, 2.0])  # COCO class 2 = car
        return np.array(boxes, dtype=np.float32).reshape(-1, 6), "yolo"

    return detect


def make_session(**overrides):
    capture = FakeCapture()
    kwargs = dict(
        lanes_path=None,
        capture_factory=lambda url: capture,
        detector_factory=lambda: boxes_detector(2),
        max_seconds=30.0,
        idle_seconds=30.0,
        window_seconds=0.15,
    )
    kwargs.update(overrides)
    session = live.LiveSession("D3-41", "https://example.test/stream.m3u8", **kwargs)
    return session, capture


class SessionTests(unittest.TestCase):
    def test_a_session_measures_and_publishes_frames(self):
        session, capture = make_session()
        session.start()
        try:
            deadline = time.time() + 15
            while (
                session.status.frame_seq < 5 or session.status.level is None
            ) and time.time() < deadline:
                time.sleep(0.01)
            status = session.poll()
            jpeg = session.frame()
        finally:
            session.stop()
            session.join(2)

        self.assertEqual(status["state"], "running")
        self.assertEqual(status["camera_id"], "D3-41")
        self.assertEqual(status["lanes"], "road")
        self.assertGreaterEqual(status["frame_seq"], 5)
        self.assertIn(status["level"], ("light", "moderate", "heavy"))
        self.assertIsNotNone(status["flow_per_min"])
        self.assertIsNotNone(status["density"])
        self.assertEqual(status["detector"], "yolo")
        self.assertTrue(jpeg and jpeg.startswith(b"\xff\xd8"), "the frame is not a JPEG")

    def test_a_dark_frame_switches_to_headlights(self):
        # A black stream means ordinary detection cannot see anything, and the session says
        # so through the detector field instead of silently reporting zero traffic.
        def dark_frames_detector():
            def detect(frame):
                return np.zeros((0, 6), dtype=np.float32), "headlights"

            return detect

        session, _ = make_session(detector_factory=dark_frames_detector)
        session.start()
        try:
            deadline = time.time() + 5
            while session.status.frame_seq < 3 and time.time() < deadline:
                time.sleep(0.01)
            status = session.poll()
        finally:
            session.stop()
            session.join(2)
        self.assertEqual(status["detector"], "headlights")

    def test_headlight_boxes_are_counted_by_the_lane_holding_most_of_them(self):
        # Tall headlight boxes drive down the frame through the road line (at 60% height).
        # They go through the area-majority path (lane_assign.MajorityLanes), and must
        # still be seen on the road and counted as crossings.
        def headlight_detector():
            state = {"n": 0}

            def detect(frame):
                state["n"] += 1
                y = (state["n"] * 6) % HEIGHT
                return np.array([[140, y - 60, 180, y, 0.9, 2.0]], dtype=np.float32), "headlights"

            return detect

        session, _ = make_session(detector_factory=headlight_detector, window_seconds=0.3)
        session.start()
        try:
            deadline = time.time() + 15
            while (session.status.flow_per_min or 0) == 0 and time.time() < deadline:
                session.poll()
                time.sleep(0.01)
            status = session.poll()
        finally:
            session.stop()
            session.join(2)
        self.assertEqual(status["detector"], "headlights")
        self.assertGreater(status["flow_per_min"], 0)
        self.assertGreater(status["density"], 0)

    def test_no_polls_stops_the_session(self):
        session, capture = make_session(idle_seconds=0.5)
        session.start()
        deadline = time.time() + 15
        while not session.is_finished() and time.time() < deadline:
            time.sleep(0.02)
        session.join(2)
        self.assertTrue(session.is_finished(), "an unpolled session must stop itself")
        self.assertEqual(session.status.state, "done")
        self.assertIn("polled", session.status.stopped_because or "")
        self.assertTrue(capture.released, "the capture is not released")

    def test_the_time_limit_stops_the_session(self):
        session, _ = make_session(max_seconds=0.5, idle_seconds=60.0)
        session.start()
        deadline = time.time() + 15
        while not session.is_finished() and time.time() < deadline:
            time.sleep(0.02)
        session.join(2)
        self.assertEqual(session.status.state, "done")
        self.assertIn("two-minute", session.status.stopped_because or "")

    def test_polling_keeps_the_session_alive(self):
        # Ten polls inside a three-second idle window: a slow machine cannot fail this.
        session, _ = make_session(idle_seconds=3.0, max_seconds=30.0)
        session.start()
        try:
            for _ in range(10):
                time.sleep(0.1)
                session.poll()
            self.assertFalse(session.is_finished(), "polling should keep it running")
        finally:
            session.stop()
            session.join(2)

    def test_stopping_records_why(self):
        session, _ = make_session()
        session.start()
        time.sleep(0.05)
        session.stop("the viewer closed the panel")
        session.join(2)
        self.assertEqual(session.status.state, "done")
        self.assertEqual(session.status.stopped_because, "the viewer closed the panel")

    def test_a_stream_that_will_not_open_fails_with_a_message(self):
        session, _ = make_session(capture_factory=lambda url: None)
        session.start()
        session.join(3)
        self.assertEqual(session.status.state, "failed")
        self.assertEqual(session.status.error, live.MESSAGES[live.STREAM_OFFLINE])
        self.assertEqual(session.status.error_code, live.STREAM_OFFLINE)

    def test_the_road_fallback_has_a_line_across_the_road(self):
        lanes = live.road_level_lanes(WIDTH, HEIGHT)
        self.assertEqual(len(lanes), 1)
        (x1, y1), (x2, y2) = lanes[0]["line_points"]
        self.assertEqual((x1, x2), (0, WIDTH))
        self.assertAlmostEqual(y1 / HEIGHT, 0.6, places=2)
        self.assertEqual(lanes[0]["polygon"].shape, (4, 2))

    def test_a_lanes_file_is_used_when_there_is_one(self):
        directory = Path(tempfile.mkdtemp())
        lanes_file = directory / "lanes_caltrans_D3-41.json"
        lanes_file.write_text(
            json.dumps(
                {
                    "camera": "D3-41",
                    "frame_size": [WIDTH, HEIGHT],
                    "lanes": [
                        {
                            "id": 1,
                            "polygon": [[0, 100], [160, 100], [160, 240], [0, 240]],
                            "line": [[0, 180], [160, 180]],
                            "directions": {"in": "in", "out": "out"},
                        }
                    ],
                }
            )
        )
        lanes = live.load_lanes_for(WIDTH, HEIGHT, lanes_file)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["id"], 1)

    def test_a_missing_lanes_file_falls_back_to_the_road(self):
        lanes = live.load_lanes_for(WIDTH, HEIGHT, Path("/nonexistent/lanes.json"))
        self.assertEqual(lanes[0]["id"], 0)


class DetectorChoiceTests(unittest.TestCase):
    """The sun picks the detector; brightness only in twilight or without coordinates."""

    def test_night_is_headlights_and_day_is_yolo_whatever_the_brightness(self):
        for current in (None, "yolo", "headlights"):
            self.assertEqual(live.choose_detector("night", 200.0, current), "headlights")
            self.assertEqual(live.choose_detector("day", 10.0, current), "yolo")

    def test_twilight_uses_brightness_with_a_margin(self):
        threshold = live.traffic_level.NIGHT_MEAN_BRIGHTNESS
        for phase in ("twilight", None):
            self.assertEqual(live.choose_detector(phase, threshold - 1, None), "headlights")
            self.assertEqual(live.choose_detector(phase, threshold + 1, None), "yolo")
            # Inside the margin a choice already made stays put.
            self.assertEqual(live.choose_detector(phase, threshold - 3, "yolo"), "yolo")
            self.assertEqual(live.choose_detector(phase, threshold + 3, "headlights"), "headlights")
            self.assertEqual(live.choose_detector(phase, threshold - 6, "yolo"), "headlights")
            self.assertEqual(live.choose_detector(phase, threshold + 6, "headlights"), "yolo")

    def test_a_session_knows_the_sun_at_its_camera(self):
        from datetime import datetime, timedelta, timezone

        pdt = timezone(timedelta(hours=-7))
        for hour, expected in ((2, "night"), (13, "day"), (18, "twilight")):
            at = datetime(2026, 10, 29, hour, 20, tzinfo=pdt)
            session, _ = make_session(location=(38.58, -121.49), now=lambda at=at: at)
            self.assertEqual(session._phase(), expected)
        session, _ = make_session()
        self.assertIsNone(session._phase())

    def test_the_manager_passes_coordinates_from_the_lookup(self):
        made = {}

        def factory(camera_id, stream_url, **kwargs):
            made.update(kwargs)
            session, _ = make_session(**{k: v for k, v in kwargs.items() if k == "location"})
            return session

        manager = live.LiveManager(
            camera_lookup=lambda camera_id: ("https://example.test/s.m3u8", None, (38.58, -121.49)),
            session_factory=factory,
            probe=lambda url: None,
        )
        result = manager.start("D3-41")
        try:
            self.assertEqual(made["location"], (38.58, -121.49))
            self.assertIn("light_source", result)
        finally:
            manager.stop(result["session_id"])


class ProbeTests(unittest.TestCase):
    """One small GET before a capture, so a dead stream never reaches OpenCV."""

    def _probe(self, status, body=b"#EXTM3U\n#EXTINF:4.0,\nseg1.ts\n"):
        return live.probe_stream("https://example.test/stream.m3u8", get=lambda url: (status, body))

    def test_a_404_is_a_dead_stream(self):
        with self.assertRaises(live.LiveUnavailable) as caught:
            self._probe(404)
        self.assertEqual(caught.exception.error_code, live.STREAM_OFFLINE)
        self.assertEqual(str(caught.exception), live.MESSAGES[live.STREAM_OFFLINE])
        # Nothing about HTTP status codes or decoders reaches a person.
        self.assertNotIn("404", str(caught.exception))
        self.assertNotIn("HTTP", str(caught.exception))

    def test_a_410_is_a_dead_stream_too(self):
        with self.assertRaises(live.LiveUnavailable) as caught:
            self._probe(410)
        self.assertEqual(caught.exception.error_code, live.STREAM_OFFLINE)

    def test_an_empty_playlist_is_a_dead_stream(self):
        with self.assertRaises(live.LiveUnavailable) as caught:
            self._probe(200, b"")
        self.assertEqual(caught.exception.error_code, live.STREAM_OFFLINE)

    def test_a_playlist_that_is_not_a_playlist_is_a_dead_stream(self):
        with self.assertRaises(live.LiveUnavailable) as caught:
            self._probe(200, b"<html><body>Not available</body></html>")
        self.assertEqual(caught.exception.error_code, live.STREAM_OFFLINE)

    def test_a_live_playlist_passes(self):
        self.assertIsNone(self._probe(200))

    def test_a_server_error_means_we_cannot_tell(self):
        with self.assertRaises(live.LiveUnavailable) as caught:
            self._probe(500)
        self.assertEqual(caught.exception.error_code, live.STREAM_UNREACHABLE)

    def test_the_probe_waits_longer_than_caltrans_takes_to_answer(self):
        # Measured from Michael's PC: a dead playlist takes 8.9 s to answer 404. An 8 s
        # timeout turned every dead camera into "unreachable" instead of "offline".
        self.assertGreater(live.PROBE_TIMEOUT_SECONDS, 9.0)
        started = time.monotonic()
        with self.assertRaises(live.LiveUnavailable) as caught:
            live.probe_stream(
                "https://example.test/stream.m3u8",
                get=lambda url: (time.sleep(0.2) or 404, b""),
            )
        self.assertEqual(caught.exception.error_code, live.STREAM_OFFLINE)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_the_timeout_is_the_one_handed_to_urlopen(self):
        seen = {}

        class Reply:
            status = 200

            def read(self, _limit):
                return b"#EXTM3U\n"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["timeout"] = timeout
            seen["agent"] = request.get_header("User-agent")
            return Reply()

        from unittest.mock import patch as mock_patch

        with mock_patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status, body = live.fetch_playlist("https://example.test/stream.m3u8")
        self.assertEqual((status, body), (200, b"#EXTM3U\n"))
        self.assertEqual(seen["timeout"], live.PROBE_TIMEOUT_SECONDS)
        self.assertIn("traffic-analyzer", seen["agent"])

    def test_a_network_error_means_we_cannot_tell(self):
        def broken(url):
            raise OSError("Connection refused")

        with self.assertRaises(live.LiveUnavailable) as caught:
            live.probe_stream("https://example.test/stream.m3u8", get=broken)
        self.assertEqual(caught.exception.error_code, live.STREAM_UNREACHABLE)
        self.assertNotIn("Connection refused", str(caught.exception))


class OfflineMemoryTests(unittest.TestCase):
    """Caltrans keeps listing dead streams; every click must not cost a round trip."""

    def _manager(self, dead=True):
        calls = {"n": 0}

        def probe(url):
            calls["n"] += 1
            if dead:
                raise live.LiveUnavailable(live.MESSAGES[live.STREAM_OFFLINE], live.STREAM_OFFLINE)

        self.now = {"t": 1000.0}
        manager = live.LiveManager(
            camera_lookup=lambda camera_id: ("https://example.test/stream.m3u8", None),
            probe=probe,
            clock=lambda: self.now["t"],
            capture_factory=lambda url: FakeCapture(),
            detector_factory=lambda: boxes_detector(1),
            max_seconds=30.0,
            idle_seconds=30.0,
            window_seconds=0.15,
        )
        return manager, calls

    def test_a_second_start_inside_fifteen_minutes_does_not_touch_the_network(self):
        manager, calls = self._manager()
        with self.assertRaises(live.LiveUnavailable) as first:
            manager.start("D3-347")
        self.assertEqual(first.exception.error_code, live.STREAM_OFFLINE)
        self.assertEqual(calls["n"], 1)
        self.assertIsNone(manager.session, "a dead stream must not leave a session behind")

        self.now["t"] += 60
        with self.assertRaises(live.LiveUnavailable) as second:
            manager.start("D3-347")
        self.assertEqual(second.exception.error_code, live.STREAM_OFFLINE)
        self.assertEqual(calls["n"], 1, "no second request to Caltrans inside the window")

        self.assertEqual(manager.offline_cameras(), ["D3-347"])
        self.assertEqual(manager.offline_details()[0]["age_s"], 60)

    def test_after_fifteen_minutes_it_asks_again(self):
        manager, calls = self._manager()
        with self.assertRaises(live.LiveUnavailable):
            manager.start("D3-347")
        self.now["t"] += live.OFFLINE_MEMORY_SECONDS + 1
        with self.assertRaises(live.LiveUnavailable):
            manager.start("D3-347")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(manager.offline_details()[0]["age_s"], 0)

    def test_the_district_feed_is_downloaded_once_an_hour(self):
        calls = []
        self.now = {"t": 5000.0}
        manager = live.LiveManager(
            probe=lambda url: None,
            clock=lambda: self.now["t"],
            capture_factory=lambda url: FakeCapture(),
            detector_factory=lambda: boxes_detector(1),
            max_seconds=30.0,
            idle_seconds=30.0,
            window_seconds=0.15,
        )

        def lookup(camera_id):
            calls.append(camera_id)
            return "https://example.test/stream.m3u8", None

        from unittest.mock import patch as mock_patch

        def run_once():
            started = manager.start("D3-41")
            manager.stop(started["session_id"])
            deadline = time.time() + 15
            while not manager.session.is_finished() and time.time() < deadline:
                time.sleep(0.02)

        with mock_patch.object(live.LiveManager, "default_lookup", side_effect=lookup):
            run_once()
            self.now["t"] += 60
            run_once()
            self.assertEqual(calls, ["D3-41"], "one download inside the hour")
            self.now["t"] += live.LOOKUP_CACHE_SECONDS + 1
            run_once()
            self.assertEqual(calls, ["D3-41", "D3-41"], "a second download after an hour")

    def test_a_camera_that_comes_back_is_forgotten_and_starts(self):
        manager, calls = self._manager(dead=False)
        manager.start("D3-41")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(manager.offline_cameras(), [])
        session = manager.session
        session.stop("test over")
        session.join(5)


class ManagerTests(unittest.TestCase):
    def _manager(self, **overrides):
        lookup = lambda camera_id: ("https://example.test/stream.m3u8", None)  # noqa: E731
        kwargs = dict(
            camera_lookup=lookup,
            probe=lambda url: None,  # no network in these tests
            capture_factory=lambda url: FakeCapture(),
            detector_factory=lambda: boxes_detector(1),
            max_seconds=30.0,
            idle_seconds=30.0,
            window_seconds=0.15,
        )
        kwargs.update(overrides)
        return live.LiveManager(**kwargs)

    def test_one_session_at_a_time_and_the_first_must_end(self):
        manager = self._manager()
        first = manager.start("D3-41")
        try:
            with self.assertRaises(live.LiveBusy) as caught:
                manager.start("D3-52")
            self.assertGreater(caught.exception.retry_after, 0)
        finally:
            manager.stop(first["session_id"])
        # A finished session frees the slot.
        deadline = time.time() + 15
        while not manager.session.is_finished() and time.time() < deadline:
            time.sleep(0.02)
        second = manager.start("D3-52")
        self.assertNotEqual(first["session_id"], second["session_id"])
        manager.stop(second["session_id"])

    def test_status_and_frames_are_only_for_the_live_session(self):
        manager = self._manager()
        started = manager.start("D3-41")
        try:
            deadline = time.time() + 15
            while manager.frame(started["session_id"]) is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(manager.frame(started["session_id"]))
            self.assertIsNone(manager.get("nope"))
            self.assertIsNone(manager.frame("nope"))
            self.assertFalse(manager.stop("nope"))
        finally:
            manager.stop(started["session_id"])

    def test_two_starts_at_once_cannot_both_win(self):
        # Both threads leave the (slow) lookup together, exactly as two clicks would. The
        # check and the set happen under one lock, so one of them gets a session and the
        # other gets 409.
        barrier = threading.Barrier(2)

        def lookup(camera_id):
            barrier.wait(5)
            return ("https://example.test/stream.m3u8", None)

        manager = self._manager(camera_lookup=lookup, idle_seconds=30.0)
        results: list[tuple[str, object]] = []

        def attempt(camera_id):
            try:
                results.append(("ok", manager.start(camera_id)["session_id"]))
            except live.LiveBusy as busy:
                results.append(("busy", busy.retry_after))

        threads = [threading.Thread(target=attempt, args=(camera_id,)) for camera_id in ("D3-41", "D3-52")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        try:
            self.assertEqual(sorted(result[0] for result in results), ["busy", "ok"], results)
        finally:
            manager.stop_all()

    def test_an_unknown_camera_is_refused(self):
        def lookup(camera_id):
            raise ValueError(f"Camera {camera_id} not found or has no live stream.")

        manager = self._manager(camera_lookup=lookup)
        with self.assertRaises(ValueError):
            manager.start("D9-999")


class EmptyStore:
    """A sweep store that has no cameras: enough to exercise the status surface."""

    def upsert_cameras(self, cameras):
        return len(cameras)

    def list_cameras(self, districts):
        return []

    def history(self, camera_id, limit=60):
        return []

    def insert_sweep(self, row):
        return None


class RouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        settings = Settings(
            database_url="postgresql://traffic:***@127.0.0.1:5432/traffic",
            worker_token=TOKEN,
            data_dir=Path(self._tmp.name),
            repo_root=Path(self._tmp.name),
        )
        manager = live.LiveManager(
            camera_lookup=lambda camera_id: ("https://example.test/stream.m3u8", None),
            probe=lambda url: None,
            capture_factory=lambda url: FakeCapture(),
            detector_factory=lambda: boxes_detector(1),
            max_seconds=30.0,
            idle_seconds=30.0,
            window_seconds=0.15,
        )
        self.worker = Worker(settings, live=manager)
        self.client = TestClient(create_app(self.worker))

    def tearDown(self):
        if self.worker.live.session is not None:
            self.worker.live.stop(self.worker.live.session.status.session_id)
        self._tmp.cleanup()

    def test_every_live_route_needs_the_token(self):
        for method, path, body in (
            ("post", "/live/start", {"camera_id": "D3-41"}),
            ("get", "/live/abc", None),
            ("get", "/live/abc/frame.jpg", None),
            ("delete", "/live/abc", None),
        ):
            with self.subTest(path):
                call = getattr(self.client, method)
                response = call(path, json=body) if body else call(path)
                self.assertEqual(response.status_code, 401)

    def test_start_status_frame_and_stop(self):
        started = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        self.assertEqual(started.status_code, 202)
        body = started.json()
        self.assertEqual(body["state"], "starting")
        session_id = body["session_id"]

        deadline = time.time() + 5
        status = {}
        while time.time() < deadline:
            status = self.client.get(f"/live/{session_id}", headers=HEADERS).json()
            if status["frame_seq"] >= 3:
                break
            time.sleep(0.02)
        self.assertEqual(status["state"], "running")
        self.assertIn(status["level"], ("light", "moderate", "heavy", None))

        frame = self.client.get(f"/live/{session_id}/frame.jpg", headers=HEADERS)
        self.assertEqual(frame.status_code, 200)
        self.assertEqual(frame.headers["content-type"], "image/jpeg")
        self.assertEqual(frame.headers["cache-control"], "no-store")
        self.assertTrue(frame.content.startswith(b"\xff\xd8"))

        stopped = self.client.delete(f"/live/{session_id}", headers=HEADERS)
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(stopped.json()["stopped"])

    def test_a_second_session_is_409_with_the_queue_time(self):
        first = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        self.assertEqual(first.status_code, 202)
        second = self.client.post("/live/start", json={"camera_id": "D3-52"}, headers=HEADERS)
        self.assertEqual(second.status_code, 409)
        body = second.json()
        self.assertEqual(body["error"], "another live analysis is running")
        self.assertGreater(body["retry_after_s"], 0)
        self.client.delete(f"/live/{first.json()['session_id']}", headers=HEADERS)

    def test_no_frame_yet_is_404(self):
        started = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        session_id = started.json()["session_id"]
        # Immediately, before the thread has analysed anything.
        response = self.client.get(f"/live/{session_id}/frame.jpg", headers=HEADERS)
        self.assertEqual(response.status_code, 404)
        self.client.delete(f"/live/{session_id}", headers=HEADERS)

    def test_an_unknown_camera_is_503_with_a_code_and_no_internal_text(self):
        self.worker.live._lookup = lambda camera_id: (_ for _ in ()).throw(
            ValueError("Camera D9-999 not found in Caltrans' district feed.")
        )
        response = self.client.post("/live/start", json={"camera_id": "D9-999"}, headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error_code"], "unavailable")
        self.assertIn("cannot be analysed right now", body["error"])
        # The ValueError text names an internal function and a district feed; it stays inside.
        self.assertNotIn("not found", body["error"])
        self.assertNotIn("ValueError", response.text)

    def test_a_dead_stream_is_503_with_the_offline_code(self):
        self.worker.live._lookup = lambda camera_id: ("https://example.test/stream.m3u8", None)

        def dead(url):
            raise live.LiveUnavailable(live.MESSAGES[live.STREAM_OFFLINE], live.STREAM_OFFLINE)

        self.worker.live._probe = dead
        response = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error_code"], "stream_offline")
        self.assertIn("isn't serving video", body["error"])

    def test_the_offline_list_names_the_cameras_that_failed(self):
        self.worker.live._lookup = lambda camera_id: ("https://example.test/stream.m3u8", None)

        def dead(url):
            raise live.LiveUnavailable(live.MESSAGES[live.STREAM_OFFLINE], live.STREAM_OFFLINE)

        self.worker.live._probe = dead
        self.client.post("/live/start", json={"camera_id": "D3-347"}, headers=HEADERS)
        response = self.client.get("/live/offline", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        # `cameras` is plain ids, the shape the notes specify; `details` carries the rest.
        self.assertEqual(response.json()["cameras"], ["D3-347"])
        self.assertEqual(response.json()["details"][0]["error_code"], "stream_offline")
        self.assertEqual(self.client.get("/live/offline").status_code, 401)

    def test_health_stays_open_but_says_only_that_live_is_busy(self):
        started = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["live"], {"busy": True})
        self.client.delete(f"/live/{started.json()['session_id']}", headers=HEADERS)

    def test_health_leaks_no_session_id_and_no_error_text(self):
        # /health is reachable through the tunnel without a token. A session id is enough to
        # watch or stop someone else's analysis, and a psycopg error can carry the database host.
        started = self.client.post("/live/start", json={"camera_id": "D3-41"}, headers=HEADERS)
        self.worker.sweep = sweep.Sweeper(
            store=EmptyStore(),
            districts=(3,),
            stills=1,
            spacing_seconds=0.0,
            sleep=lambda seconds: None,
        )
        self.worker.sweep.status.last_error = "psycopg.OperationalError: host db.internal unreachable"
        self.worker.sweep.status.next_run_at = "2026-10-29T18:30:00+00:00"
        raw = self.client.get("/health").text
        for forbidden in ("session_id", "last_error", "db.internal", "psycopg"):
            self.assertNotIn(forbidden, raw, forbidden)
        body = self.client.get("/health").json()
        self.assertEqual(body["sweep"], {"running": False, "next_run": "2026-10-29T18:30:00+00:00"})
        self.client.delete(f"/live/{started.json()['session_id']}", headers=HEADERS)


if __name__ == "__main__":
    unittest.main()
