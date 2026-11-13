"""Shared HLS (HTTP Live Streaming) helpers for the live-video recorders.

Both recorders do the same job: fetch a playlist, pick segments, append them to
one MPEG-TS file, and keep going until enough video is saved. Caltrans serves a
plain playlist; FL511 serves one behind its own player, with short-lived tokens
that can expire mid-recording, so this module also knows how to hand control back
to the caller for a fresh URL.

Everything here is network-free when `fetch` is injected, which is how the tests run.
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_PLAYLIST_BYTES = 1024 * 1024
MAX_SEGMENT_BYTES = 20 * 1024 * 1024
TS_SYNC_BYTE = 0x47
USER_AGENT = "TrafficAnalyzer/1.0"

# The box types an ISO BMFF (fragmented MP4) media segment can start with. FL511 serves
# fMP4 behind an EXT-X-MAP initialisation segment; Caltrans serves plain MPEG-TS.
ISO_BMFF_BOXES = frozenset({b"ftyp", b"styp", b"moof", b"moov", b"mdat", b"sidx", b"free"})

# What a playlist URL answers when its token has expired or the stream has moved.
EXPIRED_STATUSES = frozenset({401, 403, 404, 410})


class PlaylistUnavailable(RuntimeError):
    """The playlist answered 401/403/404/410: the token expired or the stream moved."""

    def __init__(self, url: str, status: int):
        super().__init__(f"Playlist {url} answered HTTP {status}.")
        self.url = url
        self.status = status


@dataclass(frozen=True)
class Segment:
    sequence: int
    duration: float
    url: str


@dataclass(frozen=True)
class MediaPlaylist:
    """A parsed media playlist, whatever container its segments use."""

    segments: list[Segment]
    target_duration: float
    ended: bool
    init_url: str | None = None
    is_fragmented_mp4: bool = False


@dataclass
class Recording:
    """What one recording did, for the manifest and for the caller's report."""

    started_at: datetime | None
    recorded_seconds: float
    segments: int
    missed_segments: int
    refreshes: int
    stopped_because: str


def fetch(url: str, limit: int, *, timeout: int = 20, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in EXPIRED_STATUSES:
            raise PlaylistUnavailable(url, exc.code) from exc
        raise RuntimeError(f"Request failed for {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc
    if len(body) > limit:
        raise RuntimeError(f"Response from {url} exceeds {limit} bytes.")
    return body


def parse_master_playlist(text: str, base_url: str) -> str:
    """Return the highest-bandwidth variant URL, or base_url if text is already a media playlist."""
    if "#EXT-X-STREAM-INF" not in text:
        return base_url
    variants: list[tuple[int, str]] = []
    lines = [line.strip() for line in text.splitlines()]
    for position, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attributes = dict(
            part.split("=", 1) for part in line.split(":", 1)[1].split(",") if "=" in part
        )
        uri = next((candidate for candidate in lines[position + 1 :] if candidate and not candidate.startswith("#")), "")
        if uri:
            variants.append((int(attributes.get("BANDWIDTH", "0") or 0), urllib.parse.urljoin(base_url, uri)))
    if not variants:
        raise RuntimeError(f"Master playlist {base_url} lists no video variants.")
    return max(variants)[1]


def parse_media_playlist_detailed(text: str, base_url: str) -> "MediaPlaylist":
    """Parse a media playlist, including whether it is MPEG-TS or fragmented MP4.

    A playlist with `#EXT-X-MAP` carries an initialisation segment that must be written
    before the media segments, and its segments are ISO BMFF rather than MPEG-TS.
    """
    if not text.startswith("#EXTM3U"):
        raise RuntimeError(f"{base_url} is not an HLS playlist.")
    sequence = 0
    target_duration = 10.0
    ended = False
    duration = 0.0
    init_url: str | None = None
    segmented: list[Segment] = []
    for line in (line.strip() for line in text.splitlines()):
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_url = urllib.parse.urljoin(base_url, match.group(1))
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line == "#EXT-X-ENDLIST":
            ended = True
        elif line and not line.startswith("#"):
            segmented.append(Segment(sequence, duration, urllib.parse.urljoin(base_url, line)))
            sequence += 1
            duration = 0.0
    fragmented = init_url is not None or any(
        urllib.parse.urlparse(segment.url).path.endswith((".m4s", ".m4v", ".mp4"))
        for segment in segmented
    )
    return MediaPlaylist(
        segments=segmented,
        target_duration=target_duration,
        ended=ended,
        init_url=init_url,
        is_fragmented_mp4=fragmented,
    )


def parse_media_playlist(text: str, base_url: str) -> tuple[list[Segment], float, bool]:
    """Return (segments, target duration, ended) from an HLS media playlist."""
    playlist = parse_media_playlist_detailed(text, base_url)
    return playlist.segments, playlist.target_duration, playlist.ended


def first_program_date_time(text: str) -> datetime | None:
    """The playlist's first EXT-X-PROGRAM-DATE-TIME, when the stream carries one."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return None


def looks_like_video(data: bytes, *, allow_fragmented: bool = False) -> bool:
    """Whether a segment starts with an MPEG-TS sync byte or an ISO BMFF box."""
    if data[:1] == bytes([TS_SYNC_BYTE]):
        return True
    return allow_fragmented and len(data) >= 8 and data[4:8] in ISO_BMFF_BOXES


def record_playlist(
    media_url: str,
    video_path: Path,
    duration_seconds: float,
    *,
    fetch=fetch,
    sleep=time.sleep,
    now=lambda: datetime.now(timezone.utc),
    refresh=None,
    min_refresh_seconds: float = 60.0,
    max_refreshes: int = 5,
    headers: dict[str, str] | None = None,
) -> Recording:
    """Append live segments to `video_path` until `duration_seconds` are saved.

    Recording starts at the newest segment in the playlist. When the playlist
    answers 401/403/404/410 and a `refresh` callable is given, it is asked for a
    fresh URL: at most once per `min_refresh_seconds` and at most
    `max_refreshes` times, then recording stops and keeps what it has.

    The playlist decides the container: MPEG-TS segments are appended as they come,
    and a playlist with `#EXT-X-MAP` is written as its initialisation segment followed
    by the media segments, which is a playable fragmented MP4. `headers` are sent with
    every request, which is how FL511's streams are fetched (they check the referer).
    """
    started_at: datetime | None = None
    recorded_seconds = 0.0
    segment_count = 0
    missed_segments = 0
    refreshes = 0
    next_sequence: int | None = None
    init_written = False
    fragmented = False
    last_refresh_at: float | None = None
    last_attempt: float | None = None
    stopped_because = "duration"

    # Only pass `headers` when there are some, so a caller's two-argument fetch keeps
    # working (the Caltrans recorder uses one, and so do its tests).
    extra = {"headers": headers} if headers else {}

    video_path.parent.mkdir(parents=True, exist_ok=True)
    with video_path.open("wb") as video:
        while recorded_seconds < duration_seconds:
            try:
                media_text = fetch(media_url, MAX_PLAYLIST_BYTES, **extra).decode("utf-8", "replace")
            except PlaylistUnavailable:
                if refresh is None or refreshes >= max_refreshes:
                    stopped_because = "playlist_unavailable"
                    break
                clock = time.monotonic()
                if last_refresh_at is not None and clock - last_refresh_at < min_refresh_seconds:
                    stopped_because = "refresh_too_soon"
                    break
                last_refresh_at = clock
                refreshes += 1
                media_url = refresh()
                next_sequence = None  # a new URL means a new sequence space
                continue

            playlist = parse_media_playlist_detailed(media_text, media_url)
            segments, target_duration, ended = (
                playlist.segments,
                playlist.target_duration,
                playlist.ended,
            )
            fragmented = fragmented or playlist.is_fragmented_mp4
            if playlist.init_url and not init_written:
                video.write(fetch(playlist.init_url, MAX_SEGMENT_BYTES, **extra))
                init_written = True
            if next_sequence is None and segments:
                next_sequence = segments[-1].sequence
                started_at = started_at or first_program_date_time(media_text) or now()
            for segment in segments:
                if next_sequence is None or segment.sequence < next_sequence:
                    continue
                missed_segments += segment.sequence - next_sequence
                data = fetch(segment.url, MAX_SEGMENT_BYTES, **extra)
                if not looks_like_video(data, allow_fragmented=fragmented):
                    kind = "fragmented MP4" if fragmented else "MPEG-TS"
                    raise RuntimeError(f"Segment {segment.sequence} is not {kind} video.")
                video.write(data)
                if started_at is None:
                    started_at = now()
                recorded_seconds += segment.duration
                segment_count += 1
                next_sequence = segment.sequence + 1
                if recorded_seconds >= duration_seconds:
                    break
            if ended:
                stopped_because = "ended"
                break
            if recorded_seconds >= duration_seconds:
                break
            # Re-poll at half the segment length, as the HLS spec suggests for live playlists.
            last_attempt = time.monotonic()
            sleep(max(target_duration / 2, 1.0))
            del last_attempt

    return Recording(
        started_at=started_at,
        recorded_seconds=recorded_seconds,
        segments=segment_count,
        missed_segments=missed_segments,
        refreshes=refreshes,
        stopped_because=stopped_because,
    )
