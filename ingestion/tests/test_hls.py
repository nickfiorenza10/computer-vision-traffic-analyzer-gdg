"""Tests for the shared HLS recorder, including the fragmented-MP4 playlists FL511 serves."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion import hls  # noqa: E402

MEDIA_URL = "https://stream.example.test/chan-1/index.m3u8?token=abc"

FMP4_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:10
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-MAP:URI="init123.mp4?token=abc"
#EXTINF:4.8,
seg100.mp4?token=abc
#EXTINF:4.8,
seg101.mp4?token=abc
"""

TS_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:7
#EXTINF:6.0,
seg7.ts
#EXTINF:6.0,
seg8.ts
"""


INIT_BYTES = b"\x00\x00\x00\x18ftypmp42initialisation"
SEGMENT_BYTES = b"\x00\x00\x00\x18moofmedia"

ONE_SEGMENT_FMP4 = """#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-MAP:URI="init123.mp4?token=abc"
#EXTINF:4.8,
seg100.mp4?token=abc
"""


def fetch_serving(playlist_text, segment_bytes):
    """A fetch that serves one playlist for any playlist URL and `segment_bytes` otherwise."""
    seen = []

    def fetch(url, limit, **_kwargs):
        seen.append(url)
        if "m3u8" in url:
            return playlist_text.encode()
        if "init" in url:
            return INIT_BYTES
        return segment_bytes

    fetch.seen = seen
    return fetch


class ParseTests(unittest.TestCase):
    def test_a_fragmented_playlist_reports_its_init_segment(self):
        playlist = hls.parse_media_playlist_detailed(FMP4_PLAYLIST, MEDIA_URL)
        self.assertTrue(playlist.is_fragmented_mp4)
        self.assertEqual(playlist.init_url, "https://stream.example.test/chan-1/init123.mp4?token=abc")
        self.assertEqual([segment.sequence for segment in playlist.segments], [100, 101])

    def test_an_mpeg_ts_playlist_is_not_fragmented(self):
        playlist = hls.parse_media_playlist_detailed(TS_PLAYLIST, MEDIA_URL)
        self.assertFalse(playlist.is_fragmented_mp4)
        self.assertIsNone(playlist.init_url)

    def test_the_old_tuple_form_still_works(self):
        segments, target, ended = hls.parse_media_playlist(TS_PLAYLIST, MEDIA_URL)
        self.assertEqual(len(segments), 2)
        self.assertEqual(target, 6.0)
        self.assertFalse(ended)

    def test_a_relative_init_uri_is_resolved_against_the_playlist(self):
        playlist = hls.parse_media_playlist_detailed(FMP4_PLAYLIST, MEDIA_URL)
        self.assertTrue(playlist.init_url.startswith("https://stream.example.test/chan-1/"))


class SegmentCheckTests(unittest.TestCase):
    def test_mpeg_ts_bytes_are_accepted(self):
        self.assertTrue(hls.looks_like_video(b"\x47rest"))

    def test_fmp4_bytes_need_the_flag(self):
        data = b"\x00\x00\x00\x18ftypmp42"
        self.assertFalse(hls.looks_like_video(data))
        self.assertTrue(hls.looks_like_video(data, allow_fragmented=True))

    def test_an_html_error_page_is_not_video(self):
        self.assertFalse(hls.looks_like_video(b"<!DOCTYPE html><html>", allow_fragmented=True))
        self.assertFalse(hls.looks_like_video(b"", allow_fragmented=True))


class RecordTests(unittest.TestCase):
    def test_the_init_segment_is_written_before_the_media(self):
        fetch = fetch_serving(ONE_SEGMENT_FMP4, SEGMENT_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            recording = hls.record_playlist(
                MEDIA_URL, path, 4.0, fetch=fetch, sleep=lambda _s: None,
                now=lambda: datetime(2026, 10, 29, 3, 30, tzinfo=timezone.utc),
            )
            data = path.read_bytes()
        self.assertEqual(recording.segments, 1)
        self.assertEqual(data, INIT_BYTES + SEGMENT_BYTES)
        self.assertEqual(recording.recorded_seconds, 4.8)
        self.assertTrue(any("init123.mp4" in url for url in fetch.seen))

    def test_an_mp4_segment_is_refused_for_an_mpeg_ts_playlist(self):
        fetch = fetch_serving(TS_PLAYLIST, SEGMENT_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.ts"
            with self.assertRaises(RuntimeError) as caught:
                hls.record_playlist(
                    MEDIA_URL, path, 60.0, fetch=fetch, sleep=lambda _s: None,
                )
        self.assertIn("not MPEG-TS video", str(caught.exception))

    def test_headers_are_only_sent_when_given(self):
        calls = []

        def two_argument_fetch(url, limit):
            calls.append((url, limit))
            return TS_PLAYLIST.encode() if "index" in url else bytes([0x47])

        with tempfile.TemporaryDirectory() as directory:
            hls.record_playlist(
                MEDIA_URL, Path(directory) / "clip.ts", 6.0,
                fetch=two_argument_fetch, sleep=lambda _s: None,
            )
        self.assertTrue(calls)  # a two-argument fetch must work unchanged

    def test_headers_reach_a_header_aware_fetch(self):
        seen_headers = []

        def fetch(url, limit, **kwargs):
            seen_headers.append(kwargs.get("headers"))
            return TS_PLAYLIST.encode() if "index" in url else bytes([0x47])

        with tempfile.TemporaryDirectory() as directory:
            hls.record_playlist(
                MEDIA_URL, Path(directory) / "clip.ts", 6.0,
                fetch=fetch, sleep=lambda _s: None, headers={"referer": "https://fl511.com/cctv"},
            )
        self.assertTrue(all(headers == {"referer": "https://fl511.com/cctv"} for headers in seen_headers))


if __name__ == "__main__":
    unittest.main()
