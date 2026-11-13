"""Tests for Florida live video, with no network: FL511's lists, playlists and chunks are faked."""

import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from ingestion import fl511, hls  # noqa: E402
from worker import fl511_live, live  # noqa: E402
from worker.app import LiveRequest  # noqa: E402

BLOOMINGDALE = fl511.Camera(
    camera_id="2849",
    description="Bloomingdale at US-301",
    county="Hillsborough",
    road="CR 676A",
    direction="W",
    latitude=27.893891,
    longitude=-82.333892,
    provider_timestamp="",
    image_url="https://images-dis.divas.cloud/DGI/chan-10925_h.jpg",
)


def site(site_id, latitude, longitude, video=True):
    return fl511_live.Site(site_id, latitude, longitude, video)


class SiteListTests(unittest.TestCase):
    def test_parses_the_map_icon_list_and_skips_broken_items(self):
        sites = fl511_live.parse_sites(
            {
                "item1": None,
                "item2": [
                    {"itemId": "2610", "location": [27.893891, -82.333892], "expando": {"videoEnabled": True}},
                    {"itemId": "5776", "location": [27.9, -82.4], "expando": {"videoEnabled": False}},
                    {"itemId": "x", "location": "nowhere"},
                    {"location": [1, 2]},
                ],
            }
        )
        self.assertEqual(sites, [site("2610", 27.893891, -82.333892), site("5776", 27.9, -82.4, False)])

    def test_matches_by_place_not_by_id(self):
        # fl511.com's 2849 is a different camera far away; the one at the same point is 2610.
        sites = [site("2849", 28.9, -82.0), site("2610", 27.893891, -82.333892), site("2492", 27.9, -82.33)]
        found = fl511_live.match_site(27.893891, -82.333892, "Bloomingdale at US-301", sites, lambda _id: None)
        self.assertEqual(found.site_id, "2610")

    def test_nothing_within_thirty_metres_is_no_match(self):
        sites = [site("2492", 27.8945, -82.3339)]  # about 70 m north
        self.assertIsNone(fl511_live.match_site(27.893891, -82.333892, "x", sites, lambda _id: None))

    def test_several_views_on_one_pole_are_told_apart_by_description(self):
        sites = [site("11", 27.9, -82.4), site("12", 27.9, -82.4)]
        titles = {"11": "Dale Mabry at Columbus", "12": "Dale Mabry at Hillsborough Ave"}
        found = fl511_live.match_site(27.9, -82.4, "Dale Mabry at Hillsborough Ave", sites, titles.get)
        self.assertEqual(found.site_id, "12")
        # No title matches: the first at that place, rather than nothing.
        found = fl511_live.match_site(27.9, -82.4, "Somewhere else", sites, titles.get)
        self.assertEqual(found.site_id, "11")

    def test_tooltip_description_reads_the_bold_title(self):
        def get(url, headers):
            self.assertIn("/tooltip/Cameras/2610", url)
            self.assertEqual(headers["referer"], "https://fl511.com/cctv")
            return 200, b'<td id="CameraTooltipDescriptionColumn"> <b>Bloomingdale at US-301</b> </td>'

        self.assertEqual(fl511_live.tooltip_description("2610", get=get), "Bloomingdale at US-301")
        self.assertIsNone(fl511_live.tooltip_description("2610", get=lambda url, headers: (404, b"")))


class FinderTests(unittest.TestCase):
    def finder(self, sites, root=None):
        return fl511_live.Fl511Finder(
            repo_root=root or Path("/nonexistent"),
            list_cameras=lambda: [BLOOMINGDALE],
            load_sites=lambda: sites,
            describe=lambda _id: None,
        )

    def test_finds_the_stream_marker_and_where_the_camera_is(self):
        stream, lanes, location = self.finder([site("2610", 27.893891, -82.333892)]).find("fl511-2849")
        self.assertEqual(stream, "fl511:2610")
        self.assertIsNone(lanes)
        self.assertEqual(location, (27.893891, -82.333892))

    def test_uses_an_approved_lanes_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "pipeline").mkdir()
            lanes = Path(root) / "pipeline" / "lanes_fl511_2849.json"
            lanes.write_text("{}")
            _stream, found, _where = self.finder([site("2610", 27.893891, -82.333892)], Path(root)).find("fl511-2849")
            self.assertEqual(found, lanes)

    def test_a_camera_without_video_says_so(self):
        for sites in ([], [site("2610", 27.893891, -82.333892, video=False)]):
            with self.assertRaises(fl511_live.Fl511Unavailable) as caught:
                self.finder(sites).find("fl511-2849")
            self.assertEqual(caught.exception.code, fl511_live.NO_VIDEO)

    def test_an_unknown_camera_says_so(self):
        with self.assertRaises(fl511_live.Fl511Unavailable):
            self.finder([]).find("fl511-1")

    def test_the_lists_are_fetched_once_not_per_click(self):
        calls = {"cameras": 0, "sites": 0}

        def cameras():
            calls["cameras"] += 1
            return [BLOOMINGDALE]

        def sites():
            calls["sites"] += 1
            return [site("2610", 27.893891, -82.333892)]

        finder = fl511_live.Fl511Finder(
            repo_root=Path("/x"), list_cameras=cameras, load_sites=sites, describe=lambda _id: None
        )
        finder.find("fl511-2849")
        finder.find("fl511-2849")
        self.assertEqual(calls, {"cameras": 1, "sites": 1})


MASTER = "https://dis.example:8200/chan-1_h/index.m3u8?token=t1"
MEDIA = "https://dis.example:8200/chan-1_h/xflow.m3u8?token=t1"


def media_playlist(first: int, count: int = 3) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:5", f"#EXT-X-MEDIA-SEQUENCE:{first}", '#EXT-X-MAP:URI="init.mp4"']
    for sequence in range(first, first + count):
        lines += ["#EXTINF:4.0,", f"seg{sequence}.m4s"]
    return "\n".join(lines).encode()


class FakeStream:
    """FL511's playlists and chunks, with a clock that only moves when the capture sleeps."""

    def __init__(self):
        self.now = 100.0
        self.first = 10
        self.expired = False
        self.resolves = 0
        self.headers_seen = []
        self.sleeps = []

    def resolve(self, site_id):
        self.resolves += 1
        self.expired = False
        return MASTER

    def fetch(self, url, limit, headers=None):
        self.headers_seen.append(headers)
        if self.expired:
            raise hls.PlaylistUnavailable(url, 401)
        if url == MASTER:
            return b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=720x480\nxflow.m3u8?token=t1\n"
        if "xflow" in url:
            return media_playlist(self.first)
        if url.endswith("init.mp4"):
            return b"INIT"
        return b"SEG:" + url.rsplit("seg", 1)[1].split(".")[0].encode()

    @staticmethod
    def decode(data):
        # Four frames per chunk, each stamped with its chunk number so order can be checked.
        assert data.startswith(b"INIT")
        sequence = int(data.split(b"SEG:")[1])
        return [np.full((480, 720, 3), sequence, dtype=np.uint8) for _ in range(4)]

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def capture(self):
        return fl511_live.Fl511Capture(
            "2610", resolve=self.resolve, fetch=self.fetch, decode=self.decode, clock=self.clock, sleep=self.sleep
        )


class CaptureTests(unittest.TestCase):
    def test_opens_at_the_live_edge_with_the_frame_size(self):
        stream = FakeStream()
        capture = stream.capture()
        self.assertTrue(capture.isOpened())
        self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_WIDTH), 720)
        self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_HEIGHT), 480)
        ok, frame = capture.read()
        self.assertTrue(ok)
        self.assertEqual(frame[0, 0, 0], 12)  # the newest of chunks 10-12
        # Every request carries the player page as Referer; FL511 answers 401 without it.
        self.assertTrue(all(h and h.get("referer") == "https://fl511.com/cctv" for h in stream.headers_seen))

    def test_hands_frames_out_at_the_streams_pace_then_takes_the_next_chunk(self):
        stream = FakeStream()
        capture = stream.capture()
        values = []
        for _ in range(4):
            values.append(capture.read()[1][0, 0, 0])
        # Four frames over a 4 s chunk: one a second, so three sleeps of 1 s after the first.
        self.assertEqual(stream.sleeps, [1.0, 1.0, 1.0])
        stream.first = 11  # the stream publishes chunk 13
        values.append(capture.read()[1][0, 0, 0])
        self.assertEqual(values, [12, 12, 12, 12, 13])

    def test_skips_ahead_when_the_analysis_falls_behind(self):
        stream = FakeStream()
        capture = stream.capture()
        stream.now += 3.5  # the detector took 3.5 s over the first frame
        _ok, _frame = capture.read()
        self.assertEqual(len(capture._frames), 0)  # three late frames dropped, the newest given

    def test_renews_an_expired_token_once_rather_than_failing(self):
        stream = FakeStream()
        capture = stream.capture()
        for _ in range(4):
            capture.read()
        stream.now += 30
        stream.expired = True
        stream.first = 11
        ok, frame = capture.read()
        self.assertTrue(ok)
        self.assertEqual(stream.resolves, 2)
        self.assertEqual(frame[0, 0, 0], 13)

    def test_a_token_that_keeps_expiring_stops_the_session(self):
        stream = FakeStream()
        capture = stream.capture()
        for _ in range(4):
            capture.read()
        original = stream.resolve

        def resolve_expired(site_id):
            original(site_id)
            stream.expired = True
            return MASTER

        stream.resolve = resolve_expired
        capture._resolve = resolve_expired
        stream.expired = True
        with self.assertRaises(fl511_live.Fl511Unavailable):
            capture.read()

    def test_a_stream_that_stops_publishing_fails_rather_than_waiting_forever(self):
        stream = FakeStream()
        capture = stream.capture()
        for _ in range(4):
            capture.read()
        with self.assertRaises(fl511_live.Fl511Unavailable) as caught:
            capture.read()  # chunk 12 again and again: nothing new
        self.assertEqual(caught.exception.code, "stream_offline")

    def test_a_refusal_from_fl511_is_not_retried(self):
        from ingestion import fl511_video

        def refuse(site_id):
            raise fl511_video.CaptureRefused("fl511.com answered HTTP 429")

        with self.assertRaises(fl511_live.Fl511Unavailable) as caught:
            fl511_live.Fl511Capture("2610", resolve=refuse, fetch=FakeStream().fetch, sleep=lambda s: None)
        self.assertIn("429", str(caught.exception))

    def test_decode_chunk_reads_a_real_mp4(self):
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".mp4")
        os.close(handle)
        try:
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 25, (64, 48))
            for value in range(5):
                writer.write(np.full((48, 64, 3), value * 40, dtype=np.uint8))
            writer.release()
            frames = fl511_live.decode_chunk(Path(path).read_bytes())
        finally:
            os.unlink(path)
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[0].shape, (48, 64, 3))


class WiringTests(unittest.TestCase):
    def test_open_capture_routes_markers_to_fl511(self):
        self.assertIsInstance(fl511_live.open_capture("not-a-url-anyone-serves"), cv2.VideoCapture)

    def test_the_probe_leaves_fl511_streams_alone(self):
        def never(url):
            raise AssertionError("an FL511 marker must not be fetched as a URL")

        live.probe_stream("fl511:2610", get=never)

    def test_the_lookup_turns_no_video_into_a_live_error(self):
        original = live.LiveManager._fl511
        live.LiveManager._fl511 = fl511_live.Fl511Finder(
            repo_root=Path("/x"), list_cameras=lambda: [BLOOMINGDALE], load_sites=lambda: [], describe=lambda _id: None
        )
        try:
            with self.assertRaises(live.LiveUnavailable) as caught:
                live.LiveManager.default_lookup("fl511-2849")
            self.assertEqual(caught.exception.error_code, fl511_live.NO_VIDEO)
            self.assertIn("no live video", str(caught.exception))
        finally:
            live.LiveManager._fl511 = original

    def test_the_worker_accepts_florida_ids_and_still_rejects_junk(self):
        for good in ("D3-41", "fl511-2849"):
            LiveRequest(camera_id=good)
        for bad in ("fl511-", "fl511-12a", "FL511-2849", "fl511-2849/../x", "2849"):
            with self.assertRaises(ValueError, msg=bad):
                LiveRequest(camera_id=bad)


if __name__ == "__main__":
    unittest.main()
