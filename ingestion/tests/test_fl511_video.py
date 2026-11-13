import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion import fl511_video  # noqa: E402
from ingestion.fl511_video import (  # noqa: E402
    remux_timestamps,
    CaptureRefused,
    RefreshBudget,
    acquire_slot,
    active_recordings,
    capture_block_reason,
    describe_capture_state,
    detect_block,
    pick_playlist_url,
    record_fl511_video,
    refusal_for_status,
    release_slot,
    resolve_media_playlist,
    resolve_stream_url,
)

PLAYLIST = "https://fl511.example.test/live/1267/index.m3u8"
MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x480
chunklist_w1.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=320x240
chunklist_w2.m3u8
#EXT-X-PROGRAM-DATE-TIME:2026-10-29T14:03:00Z
"""


def media_playlist(first_sequence: int, count: int, *, segment_seconds: float = 6.0, pdt: str | None = None):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-MEDIA-SEQUENCE:{first_sequence}", "#EXT-X-TARGETDURATION:6"]
    if pdt:
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt}")
    for index in range(count):
        lines.append(f"#EXTINF:{segment_seconds},")
        lines.append(f"segment{first_sequence + index}.ts")
    return "\n".join(lines) + "\n"


class SwitchTests(unittest.TestCase):
    """The confirmation switch and the expiry refuse before anything is requested."""

    def test_unconfirmed_refuses(self):
        reason = capture_block_reason(None, date(2026, 10, 29))
        self.assertIsNotNone(reason)
        self.assertIn("FDOT", reason)

    def test_confirmed_but_expired_refuses(self):
        # No end date today, so the test sets one: the refusal must survive the next permission
        # carrying a date.
        with patch.object(fl511_video, "EXPIRY_DATE", date(2026, 12, 1)):
            self.assertIsNotNone(capture_block_reason("2026-10-01", date(2026, 12, 2)))

    def test_confirmed_and_in_date_is_allowed(self):
        self.assertIsNone(capture_block_reason("2026-10-29", date(2026, 10, 29)))

    def test_no_end_date_means_no_refusal_years_later(self):
        # FDOT confirmed on 2026-10-29 that the permission has no end date.
        self.assertIsNone(fl511_video.EXPIRY_DATE)
        self.assertIsNone(capture_block_reason("2026-10-29", date(2030, 6, 1)))
        self.assertIsNone(capture_block_reason("2026-10-29", date(2040, 1, 1)))

    def test_a_date_set_again_still_refuses_after_it(self):
        with patch.object(fl511_video, "EXPIRY_DATE", date(2026, 12, 1)):
            self.assertIsNotNone(capture_block_reason("2026-10-29", date(2026, 12, 2)))
            self.assertIsNotNone(capture_block_reason("2026-12-15", date(2026, 11, 1)))

    def test_the_status_line_says_there_is_no_end_date(self):
        state = describe_capture_state(date(2030, 6, 1))
        self.assertIn("ON", state)
        self.assertIn("no end date", state)
        self.assertNotIn("EXPIRY", state)

    def test_confirmation_after_expiry_is_refused(self):
        with patch.object(fl511_video, "EXPIRY_DATE", date(2026, 12, 1)):
            self.assertIsNotNone(capture_block_reason("2026-12-15", date(2026, 11, 1)))

    def test_recording_makes_no_request_when_refused(self):
        calls = []

        def page_opener(*_args, **_kwargs):
            calls.append("page")
            return PLAYLIST

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fl511_video, "FDOT_VIDEO_CONFIRMED_ON", None
        ):
            with self.assertRaises(CaptureRefused):
                record_fl511_video(
                    "1267",
                    1,
                    Path(directory),
                    page_opener=page_opener,
                    fetch=lambda *a, **k: calls.append("fetch") or b"",
                )
        self.assertEqual(calls, [])

    def test_minutes_are_capped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            fl511_video, "FDOT_VIDEO_CONFIRMED_ON", "2026-10-29"
        ):
            with self.assertRaises(CaptureRefused):
                record_fl511_video(
                    "1267",
                    11,
                    Path(directory),
                    today=date(2026, 10, 29),
                    page_opener=lambda *a, **k: PLAYLIST,
                    fetch=lambda *a, **k: b"",
                )


TOOLTIP = b'<div id="2247-videoContainer" data-videourl="https://dis-se18.example.test:8200/chan-4473_h/index.m3u8" data-videoauth="true" data-camera-id="2247"></div>'
TOKEN_JSON = b'{"token":"71B0C6D7-7D50-4A7B-8528-861C1486D415","sourceId":"891","systemSourceId":"District 7"}'
SUFFIX_JSON = b'"?token=f2dceee6bd6b735f794fc0ffd74d0bd1c24dadc378688a56f639dceef9f6a1d2"'


class StreamUrlTests(unittest.TestCase):
    def test_resolves_the_way_the_site_player_does(self):
        seen = []

        def get(url, **kwargs):
            seen.append((url, kwargs.get("headers") or {}, kwargs.get("data")))
            if "tooltip/Cameras/2247" in url:
                return 200, TOOLTIP
            if "GetVideoUrl" in url:
                return 200, TOKEN_JSON
            return 200, SUFFIX_JSON

        url = resolve_stream_url("2247", get=get)
        self.assertEqual(
            url,
            "https://dis-se18.example.test:8200/chan-4473_h/index.m3u8"
            "?token=f2dceee6bd6b735f794fc0ffd74d0bd1c24dadc378688a56f639dceef9f6a1d2",
        )
        self.assertEqual(len(seen), 3)
        # Every call carries the referer the stream checks, and the token request is
        # the site's own JSON body posted on to divas.cloud.
        fl511_calls = [(u, h) for u, h, _d in seen if u.startswith("https://fl511.com/")]
        self.assertEqual(len(fl511_calls), 2)
        self.assertTrue(all(h.get("referer") == "https://fl511.com/cctv" for _u, h in fl511_calls))
        self.assertEqual(seen[2][2], TOKEN_JSON)

    def test_rejects_an_empty_id(self):
        with self.assertRaises(ValueError):
            resolve_stream_url("  ", get=lambda *a, **k: (200, TOOLTIP))

    def test_a_block_on_the_tooltip_stops_it(self):
        def get(url, **kwargs):
            return 200, b"<html>Please complete the CAPTCHA to continue</html>"

        with self.assertRaises(CaptureRefused):
            resolve_stream_url("2247", get=get)

    def test_a_refused_tooltip_stops_it_and_says_the_status(self):
        def get(url, **kwargs):
            return 403, b""

        with self.assertRaises(CaptureRefused) as caught:
            resolve_stream_url("2247", get=get)
        self.assertIn("403", str(caught.exception))

    def test_a_tooltip_without_a_video_url_stops_instead_of_guessing(self):
        with self.assertRaises(CaptureRefused) as caught:
            resolve_stream_url("2247", get=lambda *a, **k: (200, b"<div>nothing here</div>"))
        self.assertIn("data-videourl", str(caught.exception))

    def test_a_token_that_is_not_a_suffix_is_refused(self):
        def get(url, **kwargs):
            if "tooltip" in url:
                return 200, TOOLTIP
            if "GetVideoUrl" in url:
                return 200, TOKEN_JSON
            return 200, b'"nonsense"'

        with self.assertRaises(CaptureRefused):
            resolve_stream_url("2247", get=get)


class PlaylistPickTests(unittest.TestCase):
    def test_ignores_ts_segments_and_analytics(self):
        urls = [
            "https://fl511.com/player.js",
            "https://analytics.example.test/track.js",
            "https://cdn.example.test/live/1267/chunklist_w1.m3u8",
            "https://cdn.example.test/live/1267/segment42.ts",
        ]
        self.assertEqual(pick_playlist_url(urls), "https://cdn.example.test/live/1267/chunklist_w1.m3u8")

    def test_ignores_ad_hosts(self):
        urls = [
            "https://ads.doubleclick.net/video/ad.m3u8",
            "https://cdn.example.test/live/1267/master.m3u8",
        ]
        self.assertEqual(pick_playlist_url(urls), "https://cdn.example.test/live/1267/master.m3u8")

    def test_last_playlist_wins_because_a_fallback_follows_a_failure(self):
        urls = [
            "https://cdn.example.test/live/hi.m3u8",
            "https://cdn.example.test/live/lo.m3u8",
        ]
        self.assertEqual(pick_playlist_url(urls), "https://cdn.example.test/live/lo.m3u8")

    def test_none_found(self):
        self.assertIsNone(pick_playlist_url(["https://fl511.com/", "https://cdn.example.test/a.ts"]))


class BlockDetectionTests(unittest.TestCase):
    def test_captcha_and_login_walls_are_reported(self):
        for text in ["Please complete the CAPTCHA", "Sign in to continue", "Access Denied", "unusual traffic"]:
            with self.subTest(text=text):
                self.assertIsNotNone(detect_block(text))

    def test_a_normal_page_passes(self):
        self.assertIsNone(detect_block("<html><body>Camera 1267 live</body></html>"))

    def test_empty_page_passes(self):
        self.assertIsNone(detect_block(""))


class RefreshBudgetTests(unittest.TestCase):
    def test_five_refreshes_at_most(self):
        clock = [0.0]
        budget = RefreshBudget(clock=lambda: clock[0])
        for _ in range(5):
            self.assertTrue(budget.take())
            clock[0] += 61
        self.assertFalse(budget.take())

    def test_refreshes_are_spaced(self):
        clock = [0.0]
        budget = RefreshBudget(clock=lambda: clock[0])
        self.assertTrue(budget.take())
        clock[0] += 30
        self.assertFalse(budget.take())
        clock[0] += 31
        self.assertTrue(budget.take())


class ProcessCheckTests(unittest.TestCase):
    """The PID probe must never signal a process, and Windows must not go near os.kill."""

    def test_the_current_process_is_alive(self):
        self.assertTrue(fl511_video._pid_alive(os.getpid()))

    def test_an_unused_pid_is_not_alive(self):
        self.assertFalse(fl511_video._pid_alive(999999999))

    def test_a_nonpositive_pid_is_not_alive(self):
        self.assertFalse(fl511_video._pid_alive(0))
        self.assertFalse(fl511_video._pid_alive(-1))

    def test_windows_route_never_calls_os_kill(self):
        # os.kill(pid, 0) terminates a process on Windows, so the dispatcher must not
        # reach it there. Patching os.name lets this run on any platform.
        with patch.object(fl511_video.os, "name", "nt"), patch.object(
            fl511_video, "_windows_pid_alive", return_value=True
        ) as windows_check, patch.object(fl511_video, "_posix_pid_alive") as posix_check, patch.object(
            fl511_video.os, "kill"
        ) as kill:
            self.assertTrue(fl511_video._pid_alive(4242))
        windows_check.assert_called_once_with(4242)
        posix_check.assert_not_called()
        kill.assert_not_called()

    def test_posix_route_treats_permission_denied_as_alive(self):
        with patch.object(fl511_video.os, "kill", side_effect=PermissionError):
            self.assertTrue(fl511_video._posix_pid_alive(1))
        with patch.object(fl511_video.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(fl511_video._posix_pid_alive(1))


class ChallengeStatusTests(unittest.TestCase):
    def test_challenge_statuses_are_refused(self):
        for status in (403, 429, 503):
            with self.subTest(status=status):
                self.assertIsNotNone(refusal_for_status(status, "https://fl511.com/"))

    def test_normal_statuses_pass(self):
        for status in (None, 200, 301, 404):
            with self.subTest(status=status):
                self.assertIsNone(refusal_for_status(status, "https://fl511.com/"))


class LockTests(unittest.TestCase):
    def test_second_and_third_slot(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            fl511_video, "_pid_alive", return_value=True
        ):
            output = Path(directory)
            with patch.object(fl511_video.os, "getpid", return_value=1001):
                first = acquire_slot(output)
            with patch.object(fl511_video.os, "getpid", return_value=1002):
                second = acquire_slot(output)
            with patch.object(fl511_video.os, "getpid", return_value=1003):
                with self.assertRaises(CaptureRefused):
                    acquire_slot(output)
            self.assertEqual(len(active_recordings(output)), 2)
            release_slot(first)
            release_slot(second)
            self.assertEqual(active_recordings(output), [])

    def test_dead_process_locks_are_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            stale = output / "recording-999999999.lock"
            stale.write_text("gone")
            self.assertEqual(active_recordings(output), [])
            self.assertFalse(stale.exists())


class RemuxTests(unittest.TestCase):
    """The fMP4 timestamp step: the stream's clock is not the clip's length."""

    def _video(self, directory, name="20261029T072219Z.mp4") -> Path:
        path = Path(directory) / name
        path.write_bytes(b"raw recording")
        return path

    def test_a_successful_remux_keeps_the_original_and_returns_the_timed_copy(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            Path(command[-1]).write_bytes(b"timed recording")

            class Done:
                returncode = 0

            return Done()

        with tempfile.TemporaryDirectory() as directory:
            raw = self._video(directory)
            timed = remux_timestamps(raw, runner=runner, which=lambda name: "/usr/bin/ffmpeg")
            self.assertEqual(timed.name, "20261029T072219Z.timed.mp4")
            self.assertTrue(timed.is_file())
            self.assertTrue(raw.is_file(), "the raw recording must survive")
            self.assertEqual(raw.read_bytes(), b"raw recording")
            command = seen["command"]
            self.assertEqual(command[0], "ffmpeg")
            self.assertIn("-reset_timestamps", command)
            self.assertIn("-c", command)
            self.assertIn("copy", command)

    def test_no_ffmpeg_means_no_remux_and_no_error(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = self._video(directory)
            self.assertIsNone(remux_timestamps(raw, which=lambda name: None))
            self.assertTrue(raw.is_file())

    def test_a_failed_remux_leaves_the_original_and_no_half_written_copy(self):
        def runner(command, **kwargs):
            Path(command[-1]).write_bytes(b"")

            class Failed:
                returncode = 1

            return Failed()

        with tempfile.TemporaryDirectory() as directory:
            raw = self._video(directory)
            self.assertIsNone(remux_timestamps(raw, runner=runner, which=lambda name: "/usr/bin/ffmpeg"))
            self.assertTrue(raw.is_file())
            self.assertFalse((Path(directory) / "20261029T072219Z.timed.mp4").exists())

    def test_ffmpeg_missing_from_the_machine_is_not_an_exception(self):
        def runner(command, **kwargs):
            raise FileNotFoundError("ffmpeg")

        with tempfile.TemporaryDirectory() as directory:
            raw = self._video(directory)
            self.assertIsNone(remux_timestamps(raw, runner=runner, which=lambda name: "/usr/bin/ffmpeg"))


FMP4_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-MAP:URI="init.mp4?token=abc"
#EXTINF:4.8,
seg10.mp4?token=abc
#EXTINF:4.8,
seg11.mp4?token=abc
"""


class TimestampStepTests(unittest.TestCase):
    """The manifest has to say which file is the playable one, and keep both."""

    def _record(self, directory, remux):
        state = {"n": 0}

        def fake_fetch(url, limit, **_kwargs):
            if url == PLAYLIST:
                # A live playlist grows; a still one would be polled forever.
                state["n"] += 1
                sequence = 10 + state["n"] - 1
                return FMP4_PLAYLIST.replace("MEDIA-SEQUENCE:10", f"MEDIA-SEQUENCE:{sequence}").replace(
                    "seg10", f"seg{sequence}"
                ).replace("seg11", f"seg{sequence + 1}").encode()
            if "init" in url:
                return b"\x00\x00\x00\x18ftypinit"
            return b"\x00\x00\x00\x18moofsegment"

        with patch.object(fl511_video, "FDOT_VIDEO_CONFIRMED_ON", "2026-10-29"):
            return record_fl511_video(
                "1267",
                0.05,
                directory,
                today=date(2026, 10, 29),
                page_opener=lambda *a, **k: PLAYLIST,
                fetch=fake_fetch,
                sleep=lambda _seconds: None,
                now=lambda: datetime(2026, 10, 29, 14, 3, 5, tzinfo=timezone.utc),
                remux=remux,
            )

    def test_the_manifest_points_at_the_timed_copy_and_keeps_the_raw_one(self):
        def remux(raw):
            timed = raw.with_suffix(".timed.mp4")
            timed.write_bytes(raw.read_bytes())
            return timed

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = self._record(output, remux)
            entry = json.loads(
                [line for line in (output / "manifest.jsonl").read_text().splitlines() if line.strip()][-1]
            )
            self.assertEqual(path.name, "20261029T140305Z.timed.mp4")
            self.assertEqual(entry["video"], "fl511_1267/20261029T140305Z.timed.mp4")
            self.assertEqual(entry["video_raw"], "fl511_1267/20261029T140305Z.mp4")
            # Forward slashes: the file is written on Windows and read on Linux.
            self.assertNotIn("\\", entry["video"])
            self.assertNotIn("\\", entry["video_raw"])
            self.assertIn("reset_timestamps", entry["timestamps"])

    def test_without_ffmpeg_the_raw_file_is_the_recording_and_the_manifest_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            path = self._record(output, None)
            entry = json.loads(
                [line for line in (output / "manifest.jsonl").read_text().splitlines() if line.strip()][-1]
            )
            self.assertEqual(path.suffix, ".mp4")
            self.assertEqual(entry["video"], "fl511_1267/20261029T140305Z.mp4")
            self.assertNotIn("video_raw", entry)
            self.assertIn("not zero", entry["timestamps"])


class RecordingTests(unittest.TestCase):
    def test_manifest_fields_and_program_date_time(self):
        # A live playlist grows: each poll hands over the next segment.
        polls = {"count": 0}
        segments_seen = []

        def fake_fetch(url, limit, **_kwargs):
            if url == PLAYLIST:
                polls["count"] += 1
                first = 10 + polls["count"] - 1
                return media_playlist(first, 3, pdt="2026-10-29T14:03:00Z").encode()
            segments_seen.append(url)
            return bytes([0x47]) + b"payload"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(fl511_video, "FDOT_VIDEO_CONFIRMED_ON", "2026-10-29"):
                path = record_fl511_video(
                    "1267",
                    0.2,
                    output,
                    today=date(2026, 10, 29),
                    page_opener=lambda *a, **k: PLAYLIST,
                    fetch=fake_fetch,
                    sleep=lambda _seconds: None,
                    now=lambda: datetime(2026, 10, 29, 14, 3, 5, tzinfo=timezone.utc),
                )
            self.assertTrue(path.exists())
            manifest = [
                line for line in (output / "manifest.jsonl").read_text().splitlines() if line.strip()
            ][-1]
            import json

            entry = json.loads(manifest)
        self.assertEqual(entry["source"], "fl511-video")
        self.assertEqual(entry["camera_id"], "1267")
        self.assertEqual(entry["started_at"], "2026-10-29T14:03:00+00:00")
        self.assertEqual(entry["segments"], 2)  # starts at the newest segment already there
        self.assertEqual(entry["refreshes"], 0)
        self.assertEqual(entry["stopped_because"], "duration")
        self.assertEqual(len(segments_seen), 2)

    def test_a_lapsed_token_asks_for_a_new_playlist(self):
        first = media_playlist(1, 1)
        second = media_playlist(50, 2)
        state = {"playlist_calls": 0}

        def fake_fetch(url, limit, **_kwargs):
            if "m3u8" in url:
                state["playlist_calls"] += 1
                if state["playlist_calls"] == 1:
                    raise fl511_video.hls.PlaylistUnavailable(url, 410)
                return second.encode()
            return bytes([0x47]) + b"payload"

        opener_calls = []

        def page_opener(*_args, **_kwargs):
            opener_calls.append(1)
            return PLAYLIST

        clock = [0.0]

        with tempfile.TemporaryDirectory() as directory, patch.object(
            fl511_video, "FDOT_VIDEO_CONFIRMED_ON", "2026-10-29"
        ):
            path = record_fl511_video(
                "1267",
                0.1,
                Path(directory),
                today=date(2026, 10, 29),
                page_opener=page_opener,
                fetch=fake_fetch,
                sleep=lambda _seconds: None,
                clock=lambda: clock[0],
            )
            import json

            entry = json.loads([
                line for line in (Path(directory) / "manifest.jsonl").read_text().splitlines() if line.strip()
            ][-1])
            self.assertTrue(path.exists())
            self.assertEqual(len(opener_calls), 2)  # one to open, one to refresh
            self.assertEqual(entry["refreshes"], 1)


if __name__ == "__main__":
    unittest.main()
