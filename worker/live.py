"""Live analysis of one camera, for the map's side panel (T16).

A click on the map asks the worker to analyse a camera's own stream for a couple of minutes
and to say whether there is a lot of traffic. Browsers cannot load Caltrans streams - the
stream server answers 403 to a foreign Origin - so the panel shows annotated JPEG frames
this module produces.

The shape of a session:

* one at a time, at most two minutes, and it stops 30 s after the viewer stops polling, so a
  closed tab does not keep a GPU busy;
* the stream is read with OpenCV frame by frame, detected on the worker's device (ordinary
  YOLO, or headlight mode when the scene is dark) and tracked with the same BYTETracker the
  counting pipeline uses;
* with a committed lanes file the session counts at the lane lines; without one it counts at
  a road-level line and says so, because a lane-less camera should still show its traffic;
* every 10 s the three measurements go to `pipeline/traffic_level.py`, which is the one place
  the word "heavy" is defined, and the newest annotated frame stays in memory only.
"""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
import urllib.request
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "pipeline"))

import daylight  # noqa: E402
import traffic_level  # noqa: E402

try:  # the same two ways app.py is run: as the `worker` package, or from inside worker/
    from . import fl511_live  # noqa: E402
except ImportError:
    import fl511_live  # noqa: E402

MAX_SESSION_SECONDS = 120.0
IDLE_STOP_SECONDS = 30.0
WINDOW_SECONDS = 10.0
MAX_JPEG_BYTES = 2 * 1024 * 1024
DEFAULT_CONFIDENCE = 0.25

STARTING = "starting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


class LiveBusy(RuntimeError):
    """A session is already running; `retry_after` says how long it needs."""

    def __init__(self, retry_after: int):
        super().__init__("another live analysis is running")
        self.retry_after = retry_after


STREAM_OFFLINE = "stream_offline"
STREAM_UNREACHABLE = "stream_unreachable"

OFFLINE_STATUSES = frozenset({404, 410})
# Caltrans takes about 8.9 s to answer 404 for a dead playlist (measured 2026-10-29, curl and
# urllib agreeing), so an 8 s probe labelled every dead camera "unreachable".
PROBE_TIMEOUT_SECONDS = 15.0
PROBE_BYTES = 64 * 1024
# Caltrans drops streams while still listing them in the district feed, so a camera that
# failed is remembered for a while rather than retried on every click.
OFFLINE_MEMORY_SECONDS = 15 * 60
# The district feed costs about 1.6 s to download, and a camera's stream URL does not move
# inside an hour; a click should not pay for it twice.
LOOKUP_CACHE_SECONDS = 60 * 60

MESSAGES = {
    STREAM_OFFLINE: "Caltrans isn't serving video for this camera right now.",
    STREAM_UNREACHABLE: "The stream server could not be reached. Try again in a minute.",
    fl511_live.NO_VIDEO: "FL511 has no live video for this camera; only its still.",
}


class LiveUnavailable(RuntimeError):
    """The camera cannot be analysed right now (unknown, no stream, or no detector)."""

    def __init__(self, message: str, code: str = STREAM_UNREACHABLE):
        super().__init__(message)
        self.error_code = code


@dataclass
class LiveStatus:
    session_id: str
    camera_id: str
    state: str = STARTING
    started_at: str = ""
    elapsed_s: float = 0.0
    level: str | None = None
    flow_per_min: float | None = None
    density: float | None = None
    speed: float | None = None
    detector: str | None = None
    frame_seq: int = 0
    error: str | None = None
    error_code: str | None = None
    lanes: str = "road"
    stopped_because: str | None = None
    # Why this detector: the sun's phase at the camera, the road's brightness, the mode.
    light_source: dict | None = None

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "state": self.state,
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "level": self.level,
            "flow_per_min": None if self.flow_per_min is None else round(self.flow_per_min, 1),
            "density": None if self.density is None else round(self.density, 2),
            "speed": None if self.speed is None else round(self.speed, 2),
            "detector": self.detector,
            "frame_seq": self.frame_seq,
            "error": self.error,
            "error_code": self.error_code,
            "lanes": self.lanes,
            "stopped_because": self.stopped_because,
            "light_source": self.light_source,
        }


def road_level_lanes(width: int, height: int) -> list[dict]:
    """A road-level fallback for a camera with no lanes file: the whole frame, one line.

    It counts vehicles crossing a line across the middle of the frame and measures density
    over the whole picture, which is crude but honest, and the response says `lanes: "road"`
    so nobody mistakes it for lane-level counting.
    """
    import supervision as sv

    polygon = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.int64)
    y = int(round(height * 0.6))
    line = sv.LineZone(
        sv.Point(0, y), sv.Point(width, y), triggering_anchors=(sv.Position.BOTTOM_CENTER,)
    )
    return [
        {
            "id": 0,
            "polygon": polygon,
            "zone": sv.PolygonZone(polygon, triggering_anchors=(sv.Position.BOTTOM_CENTER,)),
            "line": line,
            "line_points": ((0, y), (width, y)),
            "directions": {"in": "in", "out": "out"},
        }
    ]


def load_lanes_for(width: int, height: int, lanes_path: Path | None) -> list[dict]:
    """The camera's lanes if it has an approved file, otherwise the road-level fallback."""
    if lanes_path is None or not lanes_path.is_file():
        return road_level_lanes(width, height)
    from track_count import load_lanes  # imported here: it pulls ultralytics in

    return load_lanes(lanes_path, width, height)


def annotate(frame: np.ndarray, tracked, lanes: list[dict], status: LiveStatus) -> np.ndarray:
    """Draw the tracked vehicles, the counting geometry and the verdict onto a copy."""
    canvas = frame.copy()
    for lane in lanes:
        cv2.polylines(canvas, [lane["polygon"]], True, (0, 200, 255), 2)
        (x1, y1), (x2, y2) = lane["line_points"]
        cv2.line(canvas, (x1, y1), (x2, y2), (0, 0, 255), 2)
    for box in tracked if tracked is not None else []:
        x1, y1, x2, y2 = (int(value) for value in box[:4])
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = str(int(box[4]))
        cv2.putText(
            canvas, label, (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1
        )

    level = status.level or "measuring"
    headline = f"{level.upper()}  {status.camera_id}"
    detail = (
        f"flow {status.flow_per_min:.0f}/min   density {status.density:.1f}   "
        f"speed {status.speed:.2f}   {status.detector or ''}"
        if status.flow_per_min is not None
        else f"measuring...  {status.detector or ''}"
    )
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 58), (0, 0, 0), -1)
    cv2.putText(canvas, headline, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(canvas, detail, (12, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1)
    return canvas


def encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buffer = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    )
    if not ok:
        raise RuntimeError("could not encode the annotated frame")
    return buffer.tobytes()


def choose_detector(phase: str | None, brightness: float, current: str | None) -> str:
    """"headlights" or "yolo": the sun first, brightness only in twilight.

    `phase` is the camera's `daylight.phase` (None without coordinates). In daylight or at
    night the answer is fixed. In twilight (or without coordinates) the smoothed brightness
    decides, with 5 levels of margin either side of the threshold once a choice is made, so
    a camera near the line doesn't flip detector frame by frame.
    """
    if phase == daylight.NIGHT:
        return "headlights"
    if phase == daylight.DAY:
        return "yolo"
    threshold = traffic_level.NIGHT_MEAN_BRIGHTNESS
    if current == "yolo" and brightness < threshold - 5:
        return "headlights"
    if current == "headlights" and brightness > threshold + 5:
        return "yolo"
    if current is not None:
        return current
    return "headlights" if traffic_level.is_night(brightness) else "yolo"


class LiveSession:
    """One running analysis: its thread, its measurements and its newest frame."""

    def __init__(
        self,
        camera_id: str,
        stream_url: str,
        *,
        lanes_path: Path | None,
        device: str = "cpu",
        model: str = "yolo26n.pt",
        imgsz: int = 640,
        max_seconds: float = MAX_SESSION_SECONDS,
        idle_seconds: float = IDLE_STOP_SECONDS,
        window_seconds: float = WINDOW_SECONDS,
        capture_factory: Callable[[str], object] | None = None,
        detector_factory: Callable[[], Callable[[np.ndarray], object]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        location: tuple[float, float] | None = None,
    ):
        self.status = LiveStatus(
            session_id=secrets.token_hex(6),
            camera_id=camera_id,
            started_at=now().isoformat(timespec="seconds"),
            lanes="lanes file" if lanes_path and Path(lanes_path).is_file() else "road",
        )
        self.stream_url = stream_url
        self.lanes_path = Path(lanes_path) if lanes_path else None
        self.device = device
        self.model = model
        self.imgsz = imgsz
        self.max_seconds = max_seconds
        self.idle_seconds = idle_seconds
        self.window_seconds = window_seconds
        self._capture_factory = capture_factory or fl511_live.open_capture
        self._detector_factory = detector_factory
        self._clock = clock
        self._now = now
        self.location = location
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = clock()
        self._last_poll = clock()
        self._jpeg: bytes | None = None
        self._crossings = 0
        self._density: list[float] = []
        self._speeds: list[float] = []
        self._window_started = clock()
        self._positions: dict[int, tuple[float, float, float]] = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"live-{self.status.session_id}", daemon=True)
        self._thread.start()

    def poll(self) -> dict:
        with self._lock:
            self._last_poll = self._clock()
            self.status.elapsed_s = self._clock() - self._started
            return self.status.as_dict()

    def stop(self, reason: str = "stopped by the viewer") -> None:
        with self._lock:
            if self.status.state in (DONE, FAILED):
                return
            self.status.state = DONE
            self.status.stopped_because = reason
            self.status.elapsed_s = self._clock() - self._started
        self._stop.set()

    def frame(self) -> bytes | None:
        with self._lock:
            self._last_poll = self._clock()
            return self._jpeg

    def is_finished(self) -> bool:
        return self.status.state in (DONE, FAILED)

    def seconds_left(self) -> int:
        return max(0, int(round(self.max_seconds - (self._clock() - self._started))))

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the work --------------------------------------------------------
    def _run(self) -> None:
        capture = None
        try:
            detector = self._make_detector()
            capture = self._capture_factory(self.stream_url)
            if capture is None or not capture.isOpened():
                raise LiveUnavailable(MESSAGES[STREAM_OFFLINE], STREAM_OFFLINE)

            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
            lanes = load_lanes_for(width, height, self.lanes_path)
            from headlights import make_tracker, track

            tracker = make_tracker()
            self._set_state(RUNNING)

            while not self._stop.is_set():
                if self._clock() - self._started >= self.max_seconds:
                    self.stop("reached the two-minute limit")
                    break
                if self._clock() - self._last_poll >= self.idle_seconds:
                    self.stop("nobody polled for 30 s")
                    break

                ok, frame = capture.read()
                if not ok or frame is None:
                    self._stop.wait(0.05)
                    if self._clock() - self._last_poll >= self.idle_seconds:
                        self.stop("nobody polled for 30 s")
                        break
                    continue

                self._analyse(frame, detector, tracker, lanes)
        except (LiveUnavailable, fl511_live.Fl511Unavailable) as error:
            # A message for a person, and a code for the panel to act on. The exception type
            # and anything a library said about a decoder stay out of both.
            with self._lock:
                self.status.state = FAILED
                self.status.error = str(error)
                self.status.error_code = getattr(error, "error_code", None) or error.code
                self.status.elapsed_s = self._clock() - self._started
        except Exception:  # noqa: BLE001 - never leak a stack trace or a library message
            with self._lock:
                self.status.state = FAILED
                self.status.error = "The live analysis stopped unexpectedly. Try again."
                self.status.error_code = "internal"
                self.status.elapsed_s = self._clock() - self._started
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:  # noqa: BLE001
                    pass

    def _make_detector(self):
        if self._detector_factory is not None:
            return self._detector_factory()
        from headlights import HeadlightDetector
        from ultralytics import YOLO

        import supervision as sv

        model = YOLO(self.model)
        headlights = HeadlightDetector()
        recent: list[float] = []
        chosen = {"name": None}

        def detect(frame: np.ndarray):
            # The sun decides day and night at the camera; in twilight a smoothed brightness
            # decides (see choose_detector).
            recent.append(traffic_level.frame_brightness(frame))
            if len(recent) > 15:
                recent.pop(0)
            smoothed = float(sorted(recent)[len(recent) // 2])
            phase = self._phase()
            chosen["name"] = choose_detector(phase, smoothed, chosen["name"])
            with self._lock:
                self.status.light_source = {
                    "phase": phase,
                    "brightness": round(smoothed, 1),
                    "mode": chosen["name"],
                }

            if chosen["name"] == "headlights":
                return headlights.detect(frame), "headlights"
            result = model.predict(
                frame,
                imgsz=self.imgsz,
                conf=DEFAULT_CONFIDENCE,
                device=self.device,
                verbose=False,
            )[0]
            detections = sv.Detections.from_ultralytics(result)
            boxes = np.column_stack(
                [
                    detections.xyxy,
                    detections.confidence if detections.confidence is not None else np.ones(len(detections)),
                    detections.class_id if detections.class_id is not None else np.zeros(len(detections)),
                ]
            ).astype(np.float32)
            return boxes.reshape(-1, 6), "yolo"

        return detect

    def _phase(self) -> str | None:
        if self.location is None:
            return None
        latitude, longitude = self.location
        return daylight.phase(latitude, longitude, self._now())

    def _analyse(self, frame, detector, tracker, lanes) -> None:
        import supervision as sv
        from headlights import track

        result = detector(frame)
        boxes, detector_name = result if isinstance(result, tuple) else (result, "yolo")
        tracked = track(tracker, np.asarray(boxes, dtype=np.float32).reshape(-1, 6), frame.shape)

        in_road = 0
        crossings = 0
        if len(tracked):
            detections = sv.Detections(
                xyxy=tracked[:, :4].astype(np.float32),
                confidence=tracked[:, 5].astype(np.float32),
                class_id=tracked[:, 6].astype(int),
                tracker_id=tracked[:, 4].astype(int),
            )
            inside_any = np.zeros(len(detections), dtype=bool)
            if detector_name == "headlights":
                # Headlight boxes are grown upward from the lights, so their bottom-centre
                # can sit over the next lane: the lane holding most of the box decides.
                night = self._majority_lanes(lanes, frame.shape)
                for index, inside in enumerate(night.members(detections)):
                    inside_any |= inside
                    crossed_in, crossed_out = night.lines[index].trigger(detections[inside])
                    crossings += int(np.count_nonzero(crossed_in)) + int(np.count_nonzero(crossed_out))
            else:
                for lane in lanes:
                    inside = lane["zone"].trigger(detections)
                    inside_any |= inside
                    crossed_in, crossed_out = lane["line"].trigger(detections[inside])
                    crossings += int(np.count_nonzero(crossed_in)) + int(np.count_nonzero(crossed_out))
            in_road = int(inside_any.sum())
            self._accumulate_speeds(tracked)

        with self._lock:
            self._crossings += crossings
            self._density.append(float(in_road))
            self.status.detector = detector_name
            self.status.frame_seq += 1
            self.status.elapsed_s = self._clock() - self._started

        self._close_window()
        canvas = annotate(frame, tracked, lanes, self.status)
        try:
            jpeg = encode_jpeg(canvas, quality=75)
        except RuntimeError:
            jpeg = None
        if jpeg is not None:
            with self._lock:
                self._jpeg = jpeg

    def _majority_lanes(self, lanes, shape):
        """The session's lanes as area masks and wide lines, built once (the lanes never change)."""
        night = getattr(self, "_night_lanes", None)
        if night is None:
            from lane_assign import MajorityLanes

            night = self._night_lanes = MajorityLanes(lanes, shape[1], shape[0])
        return night

    def _accumulate_speeds(self, tracked) -> None:
        """Box-heights per second, medians only: one parked truck should not read as stopped traffic."""
        stamp = self._clock()
        previous_stamp = getattr(self, "_speed_stamp", None)
        self._speed_stamp = stamp
        if previous_stamp is None:
            self._positions = {}
            for row in tracked:
                self._positions[int(row[4])] = (*_bottom_centre(row), float(row[3] - row[1]))
            return
        dt = max(1e-3, stamp - previous_stamp)
        for row in tracked:
            track_id = int(row[4])
            x, y = _bottom_centre(row)
            height = max(1.0, float(row[3] - row[1]))
            last = self._positions.get(track_id)
            if last is not None:
                moved = float(np.hypot(x - last[0], y - last[1]))
                self._speeds.append(moved / dt / height)
            self._positions[track_id] = (x, y, height)
        keep = {int(row[4]) for row in tracked}
        self._positions = {k: v for k, v in self._positions.items() if k in keep}

    def _close_window(self) -> None:
        elapsed = self._clock() - self._window_started
        if elapsed < self.window_seconds:
            return
        with self._lock:
            crossings = self._crossings
            density = float(np.mean(self._density)) if self._density else 0.0
            speeds = list(self._speeds)
            self._crossings = 0
            self._density = []
            self._speeds = []
        self._window_started = self._clock()
        flow = crossings * (60.0 / elapsed)
        speed = float(statistics.median(speeds)) if speeds else 0.0
        level = traffic_level.live_level(flow, density, speed)
        with self._lock:
            self.status.level = level
            self.status.flow_per_min = flow
            self.status.density = density
            self.status.speed = speed

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.status.state = state


def _bottom_centre(box) -> tuple[float, float]:
    x1, y1, x2, y2 = (float(value) for value in box[:4])
    return (x1 + x2) / 2.0, y2


PROBE_HEADERS = {
    "User-Agent": "traffic-analyzer/0.1 (research; +https://github.com/Monisha-Repos)",
}


def fetch_playlist(url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[int, bytes]:
    """GET a playlist with a project user agent. Returns (status, body) even on HTTP errors."""
    request = urllib.request.Request(url, headers=PROBE_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(PROBE_BYTES)
    except urllib.error.HTTPError as error:
        return int(error.code), b""


def probe_stream(stream_url: str, *, get: Callable[..., tuple[int, bytes]] = fetch_playlist) -> None:
    """Is Caltrans actually serving this camera? Ask before opening a capture.

    `cv2.VideoCapture` on a dead stream fails with a message about a decoder, which is what
    reached the panel as raw text. One small GET tells the truth: 404/410 or an empty
    playlist means the camera is listed but not streaming, anything a network cannot answer
    means we cannot tell yet. FL511 streams were already resolved by the lookup, and their
    playlist needs a fresh token, so they are not probed here.
    """
    if stream_url.startswith(fl511_live.PREFIX):
        return
    try:
        status, body = get(stream_url)
    except (OSError, RuntimeError, TimeoutError):
        raise LiveUnavailable(MESSAGES[STREAM_UNREACHABLE], STREAM_UNREACHABLE) from None
    if status in OFFLINE_STATUSES or (200 <= status < 300 and not body.strip()):
        raise LiveUnavailable(MESSAGES[STREAM_OFFLINE], STREAM_OFFLINE)
    if not 200 <= status < 300:
        raise LiveUnavailable(MESSAGES[STREAM_UNREACHABLE], STREAM_UNREACHABLE)
    if b"#EXTM3U" not in body[:2048]:
        raise LiveUnavailable(MESSAGES[STREAM_OFFLINE], STREAM_OFFLINE)


class LiveManager:
    """At most one live session at a time, addressed by session id."""

    def __init__(
        self,
        *,
        # (stream URL, lanes file) or (stream URL, lanes file, (latitude, longitude))
        camera_lookup: Callable[[str], tuple] | None = None,
        session_factory: Callable[..., LiveSession] = LiveSession,
        probe: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        offline_memory_seconds: float = OFFLINE_MEMORY_SECONDS,
        **session_kwargs,
    ):
        self._given_lookup = camera_lookup
        self._lookup_cache: dict[str, tuple[float, tuple]] = {}
        self._factory = session_factory
        self._probe = probe or probe_stream
        self._clock = clock
        self._offline_memory = offline_memory_seconds
        self._offline: dict[str, tuple[float, str]] = {}
        self._kwargs = session_kwargs
        self._lock = threading.Lock()
        self._session: LiveSession | None = None

    _fl511 = fl511_live.Fl511Finder(repo_root=REPO_ROOT)

    @classmethod
    def default_lookup(cls, camera_id: str) -> tuple[str, Path | None, tuple[float, float] | None]:
        """The camera's HLS URL, its approved lanes file if there is one, and where it is.

        Florida cameras ("fl511-2849") come back as an `fl511:<site>` marker: their URL needs a
        token that only lasts minutes, so the capture fetches it when the session opens.
        """
        if camera_id.startswith("fl511-"):
            try:
                return cls._fl511.find(camera_id)
            except fl511_live.Fl511Unavailable as error:
                raise LiveUnavailable(str(error), error.code) from None
        from ingestion import caltrans

        camera = caltrans.find_camera(camera_id)
        lanes = REPO_ROOT / "pipeline" / f"lanes_caltrans_{camera.camera_id}.json"
        try:
            location = (float(camera.latitude), float(camera.longitude))
        except (TypeError, ValueError):
            location = None
        return camera.stream_url, (lanes if lanes.is_file() else None), location

    def _lookup(self, camera_id: str) -> tuple:
        """The camera's stream URL and lanes, from the district feed at most once an hour."""
        if self._given_lookup is not None:
            return self._given_lookup(camera_id)
        now = self._clock()
        remembered = self._lookup_cache.get(camera_id)
        if remembered is not None and now - remembered[0] < LOOKUP_CACHE_SECONDS:
            return remembered[1]
        found = self.default_lookup(camera_id)
        self._lookup_cache[camera_id] = (now, found)
        return found

    @property
    def session(self) -> LiveSession | None:
        with self._lock:
            return self._session

    def start(self, camera_id: str, **overrides) -> dict:
        # A camera that just failed is not asked again straight away: Caltrans keeps listing
        # dead streams, and every click must not cost a round trip to a server that already
        # said no.
        remembered = self._offline.get(camera_id)
        if remembered is not None:
            at, code = remembered
            if self._clock() - at < self._offline_memory:
                raise LiveUnavailable(MESSAGES.get(code, MESSAGES[STREAM_UNREACHABLE]), code)
            self._offline.pop(camera_id, None)

        # The lookup talks to Caltrans and must not hold the lock; the check and the set must
        # happen under one acquisition, or two starts arriving together both pass the check.
        found = self._lookup(camera_id)
        stream_url, lanes = found[0], found[1]
        location = found[2] if len(found) > 2 else None
        try:
            self._probe(stream_url)
        except LiveUnavailable as error:
            self._offline[camera_id] = (self._clock(), error.error_code)
            raise
        self._offline.pop(camera_id, None)
        with self._lock:
            current = self._session
            if current is not None and not current.is_finished():
                raise LiveBusy(current.seconds_left())
            extra = {"location": location} if location is not None else {}
            session = self._factory(
                camera_id, stream_url, lanes_path=lanes, **{**self._kwargs, **extra, **overrides}
            )
            self._session = session
        session.start()
        return session.poll()

    def offline_cameras(self) -> list[str]:
        """Cameras that failed recently, for the API to serve instead of guessing."""
        now = self._clock()
        return [
            camera_id
            for camera_id, (at, _code) in sorted(self._offline.items())
            if now - at < self._offline_memory
        ]

    def offline_details(self) -> list[dict]:
        now = self._clock()
        return [
            {"camera_id": camera_id, "error_code": code, "age_s": round(now - at)}
            for camera_id, (at, code) in sorted(self._offline.items())
            if now - at < self._offline_memory
        ]

    def get(self, session_id: str) -> dict | None:
        current = self.session
        if current is None or current.status.session_id != session_id:
            return None
        return current.poll()

    def frame(self, session_id: str) -> bytes | None:
        current = self.session
        if current is None or current.status.session_id != session_id:
            return None
        return current.frame()

    def stop(self, session_id: str) -> bool:
        current = self.session
        if current is None or current.status.session_id != session_id:
            return False
        current.stop()
        return True

    def stop_all(self) -> None:
        current = self.session
        if current is not None:
            current.stop("worker shutting down")
