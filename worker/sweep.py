"""The 30-minute camera sweep: 8 stills per camera, density only (T15).

The map's heat layer is fed by two things. Live analysis answers for the camera somebody is
looking at; the sweep answers for every camera in a district, all the time, without touching
video. Every 30 minutes it takes eight stills from each camera, about a minute apart, counts
the vehicles in each and writes one row per camera, then labels the result against that
camera's own history.

Three rules shape the code:

* **Polite.** Stills are fetched with at most two requests in flight and spaced a minute
  apart, which is about how often Caltrans refreshes them.
* **Never stuck.** A camera whose feed fails is logged and skipped for the rest of that
  sweep; it is not retried in a tight loop, and one bad camera does not stop the sweep.
* **No stacking.** If the PC sleeps through a slot, one sweep runs on waking and the next is
  scheduled 30 minutes from then - never several back to back.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np

_REPO_PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(_REPO_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_REPO_PIPELINE))

import daylight  # noqa: E402  (sun times from each camera's coordinates)
import traffic_level  # noqa: E402  (the shared level thresholds)

SWEEP_INTERVAL_SECONDS = 30 * 60
STILLS_PER_SWEEP = 8
STILL_SPACING_SECONDS = 60.0
MAX_IN_FLIGHT = 2
CATALOG_REFRESH_SECONDS = 24 * 3600
MAX_STILL_BYTES = 8 * 1024 * 1024
USER_AGENT = "TrafficAnalyzer/1.0"


@dataclass
class SweepStatus:
    districts: tuple[int, ...] = (3,)
    running: bool = False
    last_started_at: str | None = None
    last_finished_at: str | None = None
    next_run_at: str | None = None
    cameras: int = 0
    swept: int = 0
    failed: int = 0
    skipped_duplicates: int = 0
    catalog_at: str | None = None
    last_error: str | None = None
    # Cameras the sun says are in daylight but whose road was dark this sweep.
    dark_in_daylight: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "districts": list(self.districts),
            "running": self.running,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "next_run_at": self.next_run_at,
            "cameras": self.cameras,
            "swept": self.swept,
            "failed": self.failed,
            "skipped_duplicates": self.skipped_duplicates,
            "catalog_at": self.catalog_at,
            "last_error": self.last_error,
            "dark_in_daylight": list(self.dark_in_daylight),
        }


def fetch_image(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_STILL_BYTES)
    if not data:
        raise RuntimeError(f"empty still from {url}")
    return data


class Sweeper:
    """Runs the sweep in the worker process, on its own thread."""

    def __init__(
        self,
        *,
        store,
        catalog: Callable[[tuple[int, ...]], list] | None = None,
        fetch: Callable[[str], bytes] = fetch_image,
        detect: Callable[[bytes], int] | None = None,
        districts: tuple[int, ...] = (3,),
        model: str = "yolo26n.pt",
        imgsz: int = 640,
        device: str = "cpu",
        stills: int = STILLS_PER_SWEEP,
        spacing_seconds: float = STILL_SPACING_SECONDS,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        catalog_refresh_seconds: float = CATALOG_REFRESH_SECONDS,
        max_in_flight: int = MAX_IN_FLIGHT,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._store = store
        self._catalog = catalog or self.default_catalog
        self._fetch = fetch
        self._detect = detect or self.default_detect
        self.status = SweepStatus(districts=tuple(districts))
        self.model = model
        self.imgsz = imgsz
        self.device = device
        self.stills = stills
        self.spacing_seconds = spacing_seconds
        self.interval_seconds = interval_seconds
        self.catalog_refresh_seconds = catalog_refresh_seconds
        self.max_in_flight = max(1, max_in_flight)
        self._now = now
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._catalog_at: datetime | None = None

    # -- catalogue -------------------------------------------------------
    @staticmethod
    def default_catalog(districts: tuple[int, ...]) -> list:
        from ingestion import caltrans

        return caltrans.list_cameras(districts)

    def refresh_catalog(self) -> int:
        """Upsert every camera in the configured districts. Returns how many were written."""
        cameras = self._catalog(self.status.districts)
        written = self._store.upsert_cameras(cameras)
        self._catalog_at = self._now()
        self.status.catalog_at = self._catalog_at.isoformat(timespec="seconds")
        return written

    def _catalog_is_stale(self) -> bool:
        return (
            self._catalog_at is None
            or (self._now() - self._catalog_at).total_seconds() >= self.catalog_refresh_seconds
        )

    # -- one sweep -------------------------------------------------------
    def run_sweep(self) -> dict:
        """Take the stills, count them, write one row per camera. Returns a summary."""
        started = self._now()
        with self._lock:
            self.status.running = True
            self.status.last_started_at = started.isoformat(timespec="seconds")
            self.status.cameras = self.status.swept = self.status.failed = 0
            self.status.skipped_duplicates = 0
            self.status.last_error = None
            self.status.dark_in_daylight = []
        try:
            if self._catalog_is_stale():
                self.refresh_catalog()
            cameras = self._store.list_cameras(self.status.districts)
            self.status.cameras = len(cameras)
            counts: dict[str, list[int]] = {camera["camera_id"]: [] for camera in cameras}
            brightness: dict[str, list[float]] = {camera["camera_id"]: [] for camera in cameras}
            alive = {camera["camera_id"]: True for camera in cameras}
            previous: dict[str, bytes] = {}

            for round_index in range(self.stills):
                self._one_round(cameras, alive, previous, counts, brightness)
                if round_index + 1 < self.stills:
                    self._sleep(self.spacing_seconds)

            swept = 0
            failed = 0
            dark_in_daylight = []
            for camera in cameras:
                camera_id = camera["camera_id"]
                if not counts.get(camera_id):
                    failed += 1
                    continue
                history = self._store.history(camera_id, limit=60)
                mean = float(np.mean(counts[camera_id]))
                darkest = float(np.mean(brightness[camera_id])) if brightness[camera_id] else 0.0
                light = traffic_level.lighting(self._phase(camera, started), darkest)
                if light == traffic_level.NIGHT:
                    level = traffic_level.NIGHT
                elif light == traffic_level.DARK_IN_DAYLIGHT:
                    level = traffic_level.UNKNOWN
                    dark_in_daylight.append(camera_id)
                else:
                    level = traffic_level.sweep_level(mean, history)
                self._store.insert_sweep(
                    {
                        "camera_id": camera_id,
                        "swept_at": started,
                        "frames": len(counts[camera_id]),
                        "vehicles_mean": mean,
                        "vehicles_max": int(max(counts[camera_id])),
                        "mean_brightness": darkest,
                        "level": level,
                        "detector": "yolo",
                    }
                )
                swept += 1
            if dark_in_daylight:
                print(
                    f"sweep: dark in daylight, labelled unknown: {', '.join(dark_in_daylight)}",
                    file=sys.stderr,
                )
            with self._lock:
                self.status.swept = swept
                self.status.failed = failed
                self.status.dark_in_daylight = dark_in_daylight
            return {"cameras": len(cameras), "swept": swept, "failed": failed}
        except Exception as error:  # noqa: BLE001 - the status route reports it
            with self._lock:
                self.status.last_error = f"{type(error).__name__}: {error}"
            return {"cameras": 0, "swept": 0, "failed": 0, "error": self.status.last_error}
        finally:
            with self._lock:
                self.status.running = False
                self.status.last_finished_at = self._now().isoformat(timespec="seconds")

    @staticmethod
    def _phase(camera: dict, when: datetime) -> str | None:
        """day, twilight or night at the camera, or None when its coordinates are unknown."""
        latitude, longitude = camera.get("latitude"), camera.get("longitude")
        if latitude is None or longitude is None:
            return None
        return daylight.phase(float(latitude), float(longitude), when)

    def _one_round(self, cameras, alive, previous, counts, brightness) -> None:
        """Fetch one still from each live camera, two at a time, and count it."""
        def handle(camera):
            camera_id = camera["camera_id"]
            if not alive.get(camera_id):
                return
            url = camera.get("image_url")
            if not url:
                alive[camera_id] = False
                return
            try:
                data = self._fetch(url)
            except Exception:  # noqa: BLE001 - a dead camera is skipped, not retried
                alive[camera_id] = False
                return
            digest = hashlib.sha256(data).digest()
            if previous.get(camera_id) == digest:
                # Caltrans refreshes about once a minute; an identical still is not news.
                with self._lock:
                    self.status.skipped_duplicates += 1
                return
            previous[camera_id] = digest
            image = self._decode(data)
            if image is None:
                alive[camera_id] = False
                return
            brightness[camera_id].append(traffic_level.frame_brightness(image))
            counts[camera_id].append(self._detect(data))

        with ThreadPoolExecutor(max_workers=self.max_in_flight) as pool:
            list(pool.map(handle, [camera for camera in cameras if alive.get(camera["camera_id"])]))

    @staticmethod
    def _decode(data: bytes):
        import cv2

        array = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        return image

    def default_detect(self, data: bytes) -> int:
        """Vehicles in one still, tiled at 640 (the snapshot_density detection)."""
        import cv2
        import sys

        from ultralytics import YOLO

        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return 0
        if not hasattr(self, "_model"):
            self._model = YOLO(self.model)
        repo_pipeline = Path(__file__).resolve().parents[1] / "pipeline"
        if str(repo_pipeline) not in sys.path:
            sys.path.insert(0, str(repo_pipeline))
        from snapshot_density import detect_boxes

        boxes = detect_boxes(self._model, image, imgsz=self.imgsz, device=self.device)
        return len(boxes)

    # -- scheduler -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="camera-sweep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        next_run = self._now()
        while not self._stop.is_set():
            with self._lock:
                self.status.next_run_at = next_run.isoformat(timespec="seconds")
            delay = (next_run - self._now()).total_seconds()
            if delay > 0:
                self._stop.wait(min(delay, 30.0))
                continue
            self.run_sweep()
            # Schedule from now, not from the missed slot: a PC that slept through three
            # hours gets one sweep, not six.
            next_run = self._now() + timedelta(seconds=self.interval_seconds)
            with self._lock:
                self.status.next_run_at = next_run.isoformat(timespec="seconds")

    def status_dict(self) -> dict:
        with self._lock:
            return self.status.as_dict()
