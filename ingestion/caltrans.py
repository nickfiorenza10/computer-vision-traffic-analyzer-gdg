"""Discover Caltrans CCTV cameras and record their live HLS video to disk."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ingestion.hls import (
    MAX_PLAYLIST_BYTES,
    MAX_SEGMENT_BYTES,
    Segment,
    fetch,
    parse_master_playlist,
    parse_media_playlist,
    record_playlist,
)

DISTRICTS = tuple(range(1, 13))
FEED_URL = "https://cwwp2.dot.ca.gov/data/d{district}/cctv/cctvStatusD{district:02d}.json"
TS_SYNC_BYTE = 0x47
USER_AGENT = "TrafficAnalyzer/1.0"


@dataclass(frozen=True)
class Camera:
    camera_id: str
    location: str
    nearby_place: str
    county: str
    route: str
    direction: str
    latitude: float
    longitude: float
    stream_url: str
    image_url: str = ""


def _fetch(url: str, limit: int) -> bytes:
    """Caltrans requests, with the camera-id context the callers expect in errors."""
    try:
        return fetch(url, limit)
    except RuntimeError as exc:
        raise RuntimeError(f"Caltrans request failed for {url}: {exc}") from exc


def _camera_from_record(district: int, record: dict) -> Camera | None:
    """Return a Camera for an in-service record with a stream, otherwise None."""
    location = record.get("location") or {}
    image_data = record.get("imageData") or {}
    stream_url = str(image_data.get("streamingVideoURL") or "").strip()
    still = image_data.get("static") or {}
    image_url = str(still.get("currentImageURL") or "").strip()
    if not image_url.startswith("https://"):
        image_url = ""
    if str(record.get("inService")).lower() != "true" or not stream_url.startswith("https://"):
        return None
    index = str(record.get("index") or "").strip()
    if not index:
        raise ValueError(f"District {district} camera record is missing its index.")
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Camera D{district}-{index} has invalid coordinates.") from exc
    return Camera(
        camera_id=f"D{district}-{index}",
        location=str(location.get("locationName") or "").strip(),
        nearby_place=str(location.get("nearbyPlace") or "").strip(),
        county=str(location.get("county") or "").strip(),
        route=str(location.get("route") or "").strip(),
        direction=str(location.get("direction") or "").strip(),
        latitude=latitude,
        longitude=longitude,
        stream_url=stream_url,
        image_url=image_url,
    )


def list_cameras(districts: tuple[int, ...] = DISTRICTS) -> list[Camera]:
    cameras: list[Camera] = []
    for district in districts:
        if district not in DISTRICTS:
            raise ValueError(f"Caltrans district must be 1-12, not {district}.")
        try:
            feed = json.loads(_fetch(FEED_URL.format(district=district), 20 * 1024 * 1024))
            records = [item["cctv"] for item in feed["data"]]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Caltrans district {district} feed is malformed: {exc}") from exc
        cameras.extend(
            camera for record in records if (camera := _camera_from_record(district, record)) is not None
        )
    return sorted(cameras, key=lambda camera: (camera.county.casefold(), camera.route, camera.location.casefold()))


def find_camera(camera_id: str) -> Camera:
    district_text, _, _index = camera_id.upper().removeprefix("D").partition("-")
    if not district_text.isdigit():
        raise ValueError(f"Camera ID {camera_id!r} must look like D7-123.")
    for camera in list_cameras((int(district_text),)):
        if camera.camera_id == camera_id.upper():
            return camera
    raise ValueError(
        f"Camera {camera_id} not found or has no live stream. "
        "Run `python -m ingestion caltrans-list` to review available cameras."
    )


def record_stream(camera: Camera, output_dir: Path, duration_seconds: float) -> Path:
    """Append live MPEG-TS segments to one .ts file until duration_seconds of video is saved.

    Recording starts at the newest segment, so `started_at` is the wall-clock time
    that segment arrived; it lags the real scene by roughly one segment (~10 s).
    The shared recorder in `ingestion.hls` does the work; this wrapper reads the
    camera's master playlist and writes the manifest.
    """
    master_text = _fetch(camera.stream_url, MAX_PLAYLIST_BYTES).decode("utf-8", "replace")
    media_url = parse_master_playlist(master_text, camera.stream_url)

    camera_dir = output_dir / f"caltrans_{camera.camera_id}"
    camera_dir.mkdir(parents=True, exist_ok=True)
    wall_clock = datetime.now(timezone.utc)
    video_path = camera_dir / (wall_clock.strftime("%Y%m%dT%H%M%SZ") + ".ts")

    try:
        recording = record_playlist(
            media_url,
            video_path,
            duration_seconds,
            fetch=_fetch,
            sleep=time.sleep,
        )
    except BaseException:
        # A partial recording is not a recording: a file nobody can date or play, and the
        # manifest has no line for it. Delete it and let the caller report the failure.
        video_path.unlink(missing_ok=True)
        raise
    if recording.segments == 0:
        video_path.unlink(missing_ok=True)
        raise RuntimeError(f"Camera {camera.camera_id} stream ended before any video was recorded.")

    manifest = {
        "started_at": (recording.started_at or wall_clock).isoformat(timespec="seconds"),
        "video": video_path.relative_to(output_dir).as_posix(),
        "recorded_seconds": round(recording.recorded_seconds, 3),
        "segments": recording.segments,
        "missed_segments": recording.missed_segments,
        **asdict(camera),
    }
    with (output_dir / "manifest.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=True) + "\n")
    return video_path
