import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion.caltrans import (
    _camera_from_record,
    parse_master_playlist,
    parse_media_playlist,
    record_stream,
)

STREAM = "https://media.example.test/D3/cam.stream/playlist.m3u8"
MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=320x240
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=409539,CODECS="avc1.66.40",RESOLUTION=640x480
chunklist_w1.m3u8
"""


def media_playlist(first_sequence, count, ended=False):
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:11", f"#EXT-X-MEDIA-SEQUENCE:{first_sequence}"]
    for sequence in range(first_sequence, first_sequence + count):
        lines += ["#EXTINF:10.0,", f"media_{sequence}.ts"]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def camera_record(**overrides):
    record = {
        "index": "1",
        "location": {
            "locationName": "Hwy 5 at Pocket",
            "nearbyPlace": "Sacramento",
            "county": "Sacramento",
            "route": "I-5",
            "direction": "Median",
            "latitude": "38.481128",
            "longitude": "-121.510528",
        },
        "inService": "true",
        "imageData": {"streamingVideoURL": STREAM},
    }
    record.update(overrides)
    return record


class CameraParsingTests(unittest.TestCase):
    def test_parse_camera_record(self):
        camera = _camera_from_record(3, camera_record())
        self.assertEqual(camera.camera_id, "D3-1")
        self.assertEqual(camera.route, "I-5")
        self.assertEqual(camera.stream_url, STREAM)

    def test_skips_cameras_without_live_video(self):
        self.assertIsNone(_camera_from_record(3, camera_record(inService="false")))
        self.assertIsNone(_camera_from_record(3, camera_record(imageData={"streamingVideoURL": ""})))

    def test_invalid_coordinates_are_rejected(self):
        record = camera_record()
        record["location"]["latitude"] = "not-a-number"
        with self.assertRaisesRegex(ValueError, "invalid coordinates"):
            _camera_from_record(3, record)


class PlaylistTests(unittest.TestCase):
    def test_master_playlist_picks_highest_bandwidth(self):
        self.assertEqual(
            parse_master_playlist(MASTER, STREAM), "https://media.example.test/D3/cam.stream/chunklist_w1.m3u8"
        )

    def test_media_playlist_is_returned_unchanged(self):
        self.assertEqual(parse_master_playlist(media_playlist(5, 1), STREAM), STREAM)

    def test_media_playlist_segments(self):
        segments, target, ended = parse_media_playlist(media_playlist(278365, 3, ended=True), STREAM)
        self.assertEqual([segment.sequence for segment in segments], [278365, 278366, 278367])
        self.assertEqual(segments[0].url, "https://media.example.test/D3/cam.stream/media_278365.ts")
        self.assertEqual(target, 11.0)
        self.assertTrue(ended)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.camera = _camera_from_record(3, camera_record())

    def fake_fetch(self, playlists):
        playlists = iter(playlists)

        def fetch(url, _limit):
            if url == STREAM:
                return MASTER.encode()
            if url.endswith(".m3u8"):
                return next(playlists).encode()
            return bytes([0x47]) + url.rsplit("/", 1)[1].encode()

        return fetch

    def test_records_new_segments_from_live_edge(self):
        playlists = [media_playlist(10, 3), media_playlist(11, 3), media_playlist(13, 3)]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.caltrans._fetch", side_effect=self.fake_fetch(playlists)
        ), patch("ingestion.caltrans.time.sleep"):
            output_dir = Path(temp_dir)
            video = record_stream(self.camera, output_dir, duration_seconds=30)
            self.assertEqual(video.read_bytes(), b"\x47media_12.ts\x47media_13.ts\x47media_14.ts")
            manifest = json.loads((output_dir / "manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(manifest["camera_id"], "D3-1")
            self.assertEqual(manifest["segments"], 3)
            self.assertEqual(manifest["recorded_seconds"], 30.0)
            self.assertEqual(manifest["missed_segments"], 0)
            # as_posix: the recording runs on Windows, the manifest is read on Linux.
            self.assertEqual(manifest["video"], video.relative_to(output_dir).as_posix())

    def test_the_manifest_path_uses_forward_slashes(self):
        # Windows writes backslashes with str(Path); a manifest is read on another machine.
        playlists = [media_playlist(10, 3), media_playlist(11, 3), media_playlist(13, 3)]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.caltrans._fetch", side_effect=self.fake_fetch(playlists)
        ), patch("ingestion.caltrans.time.sleep"):
            output_dir = Path(temp_dir)
            record_stream(self.camera, output_dir, duration_seconds=30)
            entry = json.loads((output_dir / "manifest.jsonl").read_text(encoding="utf-8").strip())
        self.assertNotIn("\\", entry["video"])
        self.assertTrue(entry["video"].startswith("caltrans_"))
        self.assertEqual(entry["video"], Path(entry["video"]).as_posix())

    def test_a_failed_recording_leaves_no_file_and_no_manifest_line(self):
        # Reproduced for real: a 403 partway through left a 0-byte .ts in the camera folder.
        # A file nobody can date or play is worse than no file, and the manifest must stay
        # honest too.
        calls = {"n": 0}

        def failing_fetch(url, _limit):
            calls["n"] += 1
            if url == STREAM:
                return MASTER.encode()
            if url.endswith(".m3u8"):
                return media_playlist(10, 2).encode()
            raise RuntimeError("Caltrans request failed: HTTP 403")

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.caltrans._fetch", side_effect=failing_fetch
        ):
            output_dir = Path(temp_dir)
            with self.assertRaises(RuntimeError):
                record_stream(self.camera, output_dir, duration_seconds=30)
            camera_dir = output_dir / f"caltrans_{self.camera.camera_id}"
            leftovers = list(camera_dir.glob("*.ts")) if camera_dir.exists() else []
            self.assertEqual(leftovers, [], "a failed recording must not leave a .ts behind")
            self.assertFalse((output_dir / "manifest.jsonl").exists())

    def test_counts_segments_that_fell_out_of_the_window(self):
        playlists = [media_playlist(10, 1), media_playlist(14, 1, ended=True)]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.caltrans._fetch", side_effect=self.fake_fetch(playlists)
        ), patch("ingestion.caltrans.time.sleep"):
            output_dir = Path(temp_dir)
            record_stream(self.camera, output_dir, duration_seconds=60)
            manifest = json.loads((output_dir / "manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(manifest["segments"], 2)
            self.assertEqual(manifest["missed_segments"], 3)

    def test_rejects_non_video_segment(self):
        def fetch(url, _limit):
            if url.endswith(".m3u8"):
                return (MASTER if url == STREAM else media_playlist(1, 1)).encode()
            return b"<html>error</html>"

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.caltrans._fetch", side_effect=fetch
        ), self.assertRaisesRegex(RuntimeError, "not MPEG-TS"):
            record_stream(self.camera, Path(temp_dir), duration_seconds=10)


if __name__ == "__main__":
    unittest.main()
