"""Florida live video: find a map camera's video on fl511.com, and read it frame by frame.

Two FL511 systems name cameras differently. The cameras the site shows come from FL511's
ArcGIS layer (`ingestion.fl511`), whose `ID` is not fl511.com's own camera id: ArcGIS 2849 is
"Bloomingdale at US-301", fl511.com 2849 is "SR-23 MM 40.1 SB" two counties away. Both list
the camera's position, and on 2026-10-29 47 of the 48 Tampa cameras sat at exactly the same
point in both. So a camera is matched by place; where several fl511.com cameras share a point
(the views of one pole), the one whose tooltip carries the same description wins.

fl511.com hands video only to its own player: `ingestion.fl511_video.resolve_stream_url` gets
a short-lived token the same way, and the playlist answers only with the player page as
Referer. OpenCV's FFmpeg cannot send that header (its capture options are ignored on this
build: the request goes out bare and gets 401), and this PC has no ffmpeg. So the chunks are
fetched here with the header, and OpenCV decodes each one from a temporary file: about 5 s of
720x480 fragmented MP4, 128 frames, decoded in about 0.1 s.

FDOT confirmed this use in writing on 2026-10-29 (see `FDOT_VIDEO_CONFIRMED_ON`). The same
rules as the recorder apply: an honest user agent, one camera at a time, and a refusal, a
CAPTCHA or a block stops the session rather than being retried around.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

MAP_ICONS_URL = "https://fl511.com/map/mapIcons/Cameras"
SITES_CACHE_SECONDS = 24 * 3600
CAMERAS_CACHE_SECONDS = 3600
# The two lists agree to the metre where they agree at all; 30 m allows for rounding.
SAME_PLACE_METRES = 30.0
# A session is two minutes; a token that dies inside it is renewed, but not in a loop.
MAX_REFRESHES = 3
REFRESH_MIN_SECONDS = 20.0
# When the analysis falls behind the stream, frames older than this are skipped, so what
# the viewer sees stays live rather than drifting further back.
MAX_LAG_SECONDS = 1.0
PREFIX = "fl511:"
NO_VIDEO = "no_video"
GZIP_MAGIC = bytes((0x1F, 0x8B))


class Fl511Unavailable(RuntimeError):
    """No video for this camera, or FL511 refused; `code` is the live panel's error code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Site:
    """One camera as fl511.com lists it on its map."""

    site_id: str
    latitude: float
    longitude: float
    video: bool


def parse_sites(payload: dict) -> list[Site]:
    """The map-icon list: `item2` is [{itemId, location: [lat, lon], expando: {videoEnabled}}]."""
    sites = []
    for item in payload.get("item2") or []:
        try:
            latitude, longitude = (float(value) for value in item["location"])
            site_id = str(item["itemId"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if site_id:
            sites.append(Site(site_id, latitude, longitude, bool((item.get("expando") or {}).get("videoEnabled"))))
    return sites


def metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    north = (a[0] - b[0]) * 111_320
    east = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(north, east)


def match_site(
    latitude: float,
    longitude: float,
    description: str,
    sites: list[Site],
    describe: Callable[[str], str | None],
) -> Site | None:
    """The fl511.com camera at this camera's place; by description when a pole has several."""
    here = (latitude, longitude)
    nearby = sorted(
        (metres_between(here, (site.latitude, site.longitude)), site.site_id, site)
        for site in sites
        if abs(site.latitude - latitude) < 0.01 and abs(site.longitude - longitude) < 0.01
    )
    nearby = [site for metres, _id, site in nearby if metres <= SAME_PLACE_METRES]
    if len(nearby) <= 1:
        return nearby[0] if nearby else None
    wanted = description.strip().casefold()
    for site in nearby:
        if (describe(site.site_id) or "").strip().casefold() == wanted:
            return site
    return nearby[0]


def tooltip_description(site_id: str, *, get=None) -> str | None:
    """The bold title of a camera's fl511.com tooltip ("Bloomingdale at US-301")."""
    from ingestion import fl511_video

    get = get or fl511_video.http_get
    status, body = get(
        fl511_video.TOOLTIP_URL_TEMPLATE.format(camera_id=site_id),
        headers={**fl511_video.STREAM_HEADERS, "accept": "*/*"},
    )
    if status != 200:
        return None
    match = re.search(rb"<b>\s*([^<]+?)\s*</b>", body)
    return match.group(1).decode("utf-8", "replace") if match else None


class Fl511Finder:
    """ArcGIS camera id -> (stream marker, lanes file, location), with both lists cached."""

    def __init__(
        self,
        *,
        repo_root: Path,
        list_cameras: Callable[[], list] | None = None,
        load_sites: Callable[[], list[Site]] | None = None,
        describe: Callable[[str], str | None] = tooltip_description,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._repo_root = repo_root
        self._list_cameras = list_cameras or _arcgis_cameras
        self._load_sites = load_sites or _fl511_sites
        self._describe = describe
        self._clock = clock
        self._cameras: tuple[float, dict] | None = None
        self._sites: tuple[float, list[Site]] | None = None

    def find(self, camera_id: str) -> tuple[str, Path | None, tuple[float, float]]:
        from ingestion import fl511_video

        refused = fl511_video.capture_block_reason(fl511_video.FDOT_VIDEO_CONFIRMED_ON)
        if refused:
            raise Fl511Unavailable(refused, NO_VIDEO)
        arcgis_id = camera_id.removeprefix("fl511-")
        camera = self._camera(arcgis_id)
        if camera is None:
            raise Fl511Unavailable("FL511 doesn't list this camera any more.", NO_VIDEO)
        site = match_site(camera.latitude, camera.longitude, camera.description, self._all_sites(), self._describe)
        if site is None or not site.video:
            raise Fl511Unavailable("FL511 has no live video for this camera; only its still.", NO_VIDEO)
        lanes = self._repo_root / "pipeline" / f"lanes_fl511_{arcgis_id}.json"
        return f"{PREFIX}{site.site_id}", (lanes if lanes.is_file() else None), (camera.latitude, camera.longitude)

    def _camera(self, arcgis_id: str):
        now = self._clock()
        if self._cameras is None or now - self._cameras[0] >= CAMERAS_CACHE_SECONDS:
            self._cameras = (now, {camera.camera_id: camera for camera in self._list_cameras()})
        return self._cameras[1].get(arcgis_id)

    def _all_sites(self) -> list[Site]:
        now = self._clock()
        if self._sites is None or now - self._sites[0] >= SITES_CACHE_SECONDS:
            self._sites = (now, self._load_sites())
        return self._sites[1]


def _arcgis_cameras() -> list:
    from ingestion import fl511

    return fl511.list_cameras(include_highways=True)


def _fl511_sites() -> list[Site]:
    from ingestion import fl511_video, hls

    body = hls.fetch(
        MAP_ICONS_URL,
        8 * 1024 * 1024,
        headers={**fl511_video.STREAM_HEADERS, "accept": "application/json"},
    )
    # fl511.com gzips this list whether or not it was asked to (checked 2026-10-29).
    if body[:2] == GZIP_MAGIC:
        body = gzip.decompress(body)
    return parse_sites(json.loads(body.decode("utf-8")))


def decode_chunk(data: bytes) -> list[np.ndarray]:
    """Every frame of one fragmented-MP4 chunk (init segment first)."""
    handle, path = tempfile.mkstemp(suffix=".mp4")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        capture = cv2.VideoCapture(path)
        frames = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                frames.append(frame)
        finally:
            capture.release()
        return frames
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class Fl511Capture:
    """Enough of `cv2.VideoCapture` for a live session, over FL511's tokened HLS.

    Frames are handed out at the stream's own pace (a chunk's frames spread over its
    duration), so speeds measured per second stay right; a session that falls behind
    skips ahead instead of drifting into the past.
    """

    def __init__(
        self,
        site_id: str,
        *,
        resolve: Callable[[str], str] | None = None,
        fetch: Callable[..., bytes] | None = None,
        decode: Callable[[bytes], list[np.ndarray]] = decode_chunk,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        from ingestion import fl511_video, hls

        self._hls = hls
        self._site_id = site_id
        self._resolve = resolve or fl511_video.resolve_stream_url
        self._fetch = fetch or hls.fetch
        self._headers = dict(fl511_video.STREAM_HEADERS)
        self._decode = decode
        self._clock = clock
        self._sleep = sleep
        self._frames: deque[tuple[float, np.ndarray]] = deque()
        self._next_due = 0.0
        self._last_sequence = -1
        self._refreshes = 0
        self._refreshed_at = -math.inf
        self._open = False
        self._size = (0, 0)
        self._media_url = ""
        self._init = b""
        self._target = 5.0
        try:
            self._connect()
            # The first chunk now, so the session knows the frame size before it asks.
            self._fill(initial=True)
            self._open = bool(self._frames)
        except Fl511Unavailable:
            raise
        except (RuntimeError, OSError, ValueError):
            self._open = False

    # -- the cv2.VideoCapture surface ----------------------------------------
    def isOpened(self) -> bool:  # noqa: N802 - cv2's name
        return self._open

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._size[0])
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._size[1])
        return 0.0

    def release(self) -> None:
        self._open = False
        self._frames.clear()

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._open:
            return False, None
        if not self._frames:
            try:
                self._fill()
            except (RuntimeError, OSError, ValueError) as error:
                if isinstance(error, Fl511Unavailable):
                    raise
                raise Fl511Unavailable(
                    "The connection to FL511 dropped. Try again in a minute.", "stream_unreachable"
                ) from None
            if not self._frames:
                return False, None
        now = self._clock()
        # Behind by more than MAX_LAG_SECONDS: drop to the newest frames.
        while len(self._frames) > 1 and now - self._frames[0][0] > MAX_LAG_SECONDS:
            self._frames.popleft()
        due, frame = self._frames.popleft()
        if due > now:
            self._sleep(due - now)
        return True, frame

    # -- the stream ----------------------------------------------------------
    def _connect(self) -> None:
        from ingestion import fl511_video

        try:
            url = self._resolve(self._site_id)
        except fl511_video.CaptureRefused as error:
            raise Fl511Unavailable(f"FL511 refused the video: {error}", "stream_unreachable") from None
        master = self._fetch(url, self._hls.MAX_PLAYLIST_BYTES, headers=self._headers).decode("utf-8", "replace")
        self._media_url = self._hls.parse_master_playlist(master, url)
        playlist = self._playlist()
        self._init = (
            self._fetch(playlist.init_url, self._hls.MAX_SEGMENT_BYTES, headers=self._headers)
            if playlist.init_url
            else b""
        )

    def _playlist(self):
        text = self._fetch(self._media_url, self._hls.MAX_PLAYLIST_BYTES, headers=self._headers)
        playlist = self._hls.parse_media_playlist_detailed(text.decode("utf-8", "replace"), self._media_url)
        self._target = playlist.target_duration or self._target
        return playlist

    def _refresh(self) -> None:
        """A new token: the old one expired (FL511 answers 401/403 for that)."""
        now = self._clock()
        if self._refreshes >= MAX_REFRESHES or now - self._refreshed_at < REFRESH_MIN_SECONDS:
            raise Fl511Unavailable(
                "FL511 stopped serving this camera's video. Try again in a minute.", "stream_unreachable"
            )
        self._refreshes += 1
        self._refreshed_at = now
        try:
            self._connect()
        except self._hls.PlaylistUnavailable:
            raise Fl511Unavailable(
                "FL511 stopped serving this camera's video. Try again in a minute.", "stream_unreachable"
            ) from None

    def _fill(self, *, initial: bool = False) -> None:
        """Queue the next new chunk's frames, waiting for the stream to publish one."""
        waited = 0.0
        while self._open or initial:
            try:
                playlist = self._playlist()
            except self._hls.PlaylistUnavailable:
                self._refresh()
                continue
            fresh = [segment for segment in playlist.segments if segment.sequence > self._last_sequence]
            if initial:
                # Start at the live edge, not at the oldest chunk the playlist still lists.
                fresh = fresh[-1:]
            if fresh:
                segment = fresh[0]
                try:
                    data = self._fetch(segment.url, self._hls.MAX_SEGMENT_BYTES, headers=self._headers)
                except self._hls.PlaylistUnavailable:
                    self._refresh()
                    continue
                self._last_sequence = segment.sequence
                frames = self._decode(self._init + data)
                if not frames:
                    continue
                self._size = (frames[0].shape[1], frames[0].shape[0])
                step = (segment.duration or self._target) / len(frames)
                start = max(self._clock(), self._next_due)
                for index, frame in enumerate(frames):
                    self._frames.append((start + index * step, frame))
                self._next_due = start + len(frames) * step
                return
            if waited >= 3 * self._target:
                raise Fl511Unavailable("FL511's stream for this camera stopped updating.", "stream_offline")
            pause = max(0.5, self._target / 2)
            self._sleep(pause)
            waited += pause


def open_capture(stream: str):
    """A capture for a live session: FL511's markers get the reader above, the rest OpenCV."""
    if stream.startswith(PREFIX):
        return Fl511Capture(stream[len(PREFIX):])
    return cv2.VideoCapture(stream)
