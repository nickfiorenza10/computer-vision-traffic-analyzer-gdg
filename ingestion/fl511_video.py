"""Record FL511 live video using the site's own stream address.

FL511 serves live video only to its own page, using short-lived tokens. The site has
no per-camera page address: the player is the camera list at `PLAYER_PAGE`, and a
camera is opened by clicking its row. What the page then does, and what this module
does instead of driving a browser, is three plain requests:

1. `GET /tooltip/Cameras/<id>` returns the row's fragment, which carries the camera's
   `data-videourl` - the base playlist on `dis-*.divas.cloud`.
2. `GET /Camera/GetVideoUrl?imageId=<id>` returns `{token, sourceId, systemSourceId}`.
3. `POST` that JSON to divas.cloud's `GetSecureTokenUriBySourceId` returns the
   `?token=...` suffix the player appends to the base URL.

The resulting playlist only answers with `Referer: https://fl511.com/cctv`, which is
the header a browser sends from that page; both the playlist and its segments are
fragmented MP4, so the file is written as `.mp4` with its `#EXT-X-MAP` init segment
first.

**This stays switched off until FDOT's written confirmation is on record.** The
research authorization that covers FL511 covers snapshots; FL511's own policy
limits live video, so `FDOT_VIDEO_CONFIRMED_ON` must stay `None` until the written
confirmation exists. Only Claude sets it, in its own commit, on Michael's word.
Nothing else may override it: there is no flag and no environment variable.

    python -m ingestion fl511-record --camera-id 1267 --minutes 5

Hard limits, which are rules rather than settings: one camera per process, at
most two recordings at once, ten minutes each, no disguising automation, and on a
CAPTCHA, bot check, login or a 403/429 from the page itself it stops and says so.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from ingestion import hls

# Date FDOT confirmed in writing that automated capture of fl511.com video is allowed for
# this research project. None = not confirmed: capture is refused. FDOT confirmed by email
# on 2026-10-29 both the scripted-browser method and the direct method used by
# resolve_stream_url() (FL511's camera endpoints, the divas.cloud token service, and the
# Referer that the /cctv page sends). The emails are kept outside the repo.
FDOT_VIDEO_CONFIRMED_ON: str | None = "2026-10-29"

# No end date. FDOT confirmed by email on 2026-10-29 that the project may use FL511 with no
# end date, public display included; that replaced the 2026-12-01 limit. The check stays for a
# date being set again (a different permission may carry one): `None` means no expiry, a date
# means nothing records on or after it.
EXPIRY_DATE: date | None = None

# The page that plays video. FL511 has no per-camera URL: `?camera<id>`, `#camera-<id>`
# and `/cctv/<id>` were all checked in a browser and none of them opens a camera (the
# last 404s, the others land on the map or the list with nothing selected). A camera is
# chosen by clicking its card on this page, so the page also serves as the referer the
# stream checks.
PLAYER_PAGE = "https://fl511.com/cctv"
TOOLTIP_URL_TEMPLATE = "https://fl511.com/tooltip/Cameras/{camera_id}?lang=en&noCss=true"
TOKEN_URL_TEMPLATE = "https://fl511.com/Camera/GetVideoUrl?imageId={camera_id}"
SECURE_URI_ENDPOINT = "https://divas.cloud/VDS-API/SecureTokenUri/GetSecureTokenUriBySourceId"
STREAM_HEADERS = {"referer": PLAYER_PAGE}

# The live encoder's timestamps are continuous from the encoder's own start, so a 60 s
# recording can report 14 hours of duration. One remux step fixes it without touching a frame.
FFMPEG_TIMEOUT_SECONDS = 180.0

MAX_MINUTES = 10.0
MAX_ACTIVE_RECORDINGS = 2
REFRESH_MIN_SECONDS = 60.0
MAX_REFRESHES = 5
PAGE_TIMEOUT_SECONDS = 45.0
LOCK_PREFIX = "recording-"
LOCK_SUFFIX = ".lock"

SOURCE = "fl511-video"

# Hosts that also serve .m3u8 files but are not the camera stream.
AD_HOSTS = ("doubleclick.net", "googlesyndication.com", "googleadservices.com", "adnxs.com", "scorecardresearch.com")
BLOCK_MARKERS = (
    "captcha",
    "are you a human",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "sign in to continue",
    "log in to continue",
)


class CaptureRefused(RuntimeError):
    """Capture is not allowed right now; the message says why."""


# Statuses that mean "the site is challenging this browser", not "try again later".
BLOCK_STATUSES = frozenset({403, 429, 503})


def refusal_for_status(status: int | None, url: str) -> str | None:
    """A refusal message for a challenge status, or None for a normal page."""
    if status is not None and status in BLOCK_STATUSES:
        return (
            f"fl511.com answered HTTP {status} for {url}. Treating that as a block: stopping, "
            "not retrying, and reporting it to Claude."
        )
    return None


def capture_block_reason(confirmed_on: str | None, today: date | None = None) -> str | None:
    """Why capture is refused, or None when it is allowed. Pure: no clock, no network."""
    today = today or date.today()
    if confirmed_on is None:
        return (
            "FL511 live video capture is switched off: FDOT has not confirmed in writing yet "
            "(ingestion/fl511_video.py, FDOT_VIDEO_CONFIRMED_ON). Snapshots are unaffected."
        )
    try:
        confirmed = date.fromisoformat(confirmed_on)
    except ValueError:
        return f"FDOT_VIDEO_CONFIRMED_ON is not an ISO date: {confirmed_on!r}."
    if EXPIRY_DATE is not None:
        if today > EXPIRY_DATE:
            return (
                f"The FL511 research authorization ended on {EXPIRY_DATE.isoformat()}; "
                "capture is refused. Ask Michael about a renewal."
            )
        if confirmed > EXPIRY_DATE:
            return f"FDOT_VIDEO_CONFIRMED_ON ({confirmed_on}) is after the authorization ends ({EXPIRY_DATE})."
    return None


def _camera(camera_id: str) -> str:
    camera = str(camera_id).strip()
    if not camera:
        raise ValueError("camera_id must not be empty")
    return camera


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    limit: int = hls.MAX_PLAYLIST_BYTES,
) -> tuple[int, bytes]:
    """One HTTP request, returning (status, body) and never raising on an HTTP error."""
    request_headers = {"user-agent": hls.USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(limit)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(limit) or b""
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def resolve_stream_url(camera_id: str, *, get=http_get) -> str:
    """The camera's HLS playlist URL, resolved the way the site's own player resolves it.

    Three calls, each of them the site's own: the tooltip carries the base playlist, the
    camera endpoint carries the token request, and divas.cloud trades that token for the
    `?token=...` suffix. A block or a refusal on any of them stops the recording.
    """
    camera = _camera(camera_id)
    headers = {**STREAM_HEADERS, "accept": "*/*"}

    status, tooltip = get(TOOLTIP_URL_TEMPLATE.format(camera_id=camera), headers=headers)
    if status != 200:
        raise CaptureRefused(f"fl511.com returned HTTP {status} for camera {camera}'s tooltip.")
    block = detect_block(tooltip.decode("utf-8", "replace"))
    if block:
        raise CaptureRefused(block)
    match = re.search(rb'data-videourl="([^"]+)"', tooltip)
    if not match:
        raise CaptureRefused(
            f"The tooltip for camera {camera} carried no data-videourl; the site may have "
            "changed. Tell Claude rather than guessing a new endpoint."
        )
    base = match.group(1).decode("ascii", "replace")

    status, token_request = get(
        TOKEN_URL_TEMPLATE.format(camera_id=camera),
        headers={**headers, "accept": "application/json"},
    )
    if status != 200:
        raise CaptureRefused(f"fl511.com returned HTTP {status} asking for camera {camera}'s video token.")

    status, suffix_body = get(
        SECURE_URI_ENDPOINT,
        headers={"content-type": "application/json", "accept": "application/json"},
        data=token_request,
    )
    if status != 200:
        raise CaptureRefused(f"divas.cloud returned HTTP {status} for camera {camera}'s token.")
    try:
        suffix = json.loads(suffix_body.decode("utf-8", "replace"))
    except ValueError as error:
        raise CaptureRefused(f"divas.cloud returned something that is not JSON for camera {camera}.") from error
    if not isinstance(suffix, str) or not suffix.startswith("?"):
        raise CaptureRefused(f"divas.cloud returned no token suffix for camera {camera}: {suffix!r}.")
    return base + suffix


def pick_playlist_url(request_urls) -> str | None:
    """Choose the HLS playlist the player is streaming, from the URLs it requested.

    Variants and the master playlist are both playlists; the last one requested
    wins, because a player that falls back to a lower variant asks for the next
    one after the failure. Ad and analytics hosts are ignored.
    """
    candidates = []
    for url in request_urls:
        if ".m3u8" not in url.lower():
            continue
        lowered = url.lower()
        if any(host in lowered for host in AD_HOSTS):
            continue
        candidates.append(url)
    return candidates[-1] if candidates else None


def detect_block(page_text: str) -> str | None:
    """A CAPTCHA, bot check or login wall in the page, or None when it looks normal."""
    lowered = (page_text or "").lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return (
                f"fl511.com showed a block: {marker!r}. Not retrying, not working around it; "
                "tell Claude so the team can decide."
            )
    return None


@dataclass
class RefreshBudget:
    """At most `max_refreshes` page re-opens, spaced by at least `min_seconds`."""

    max_refreshes: int = MAX_REFRESHES
    min_seconds: float = REFRESH_MIN_SECONDS
    clock: Callable[[], float] = time.monotonic
    used: int = field(default=0)
    last_at: float | None = field(default=None)

    def take(self) -> bool:
        if self.used >= self.max_refreshes:
            return False
        now = self.clock()
        if self.last_at is not None and now - self.last_at < self.min_seconds:
            return False
        self.used += 1
        self.last_at = now
        return True


def _posix_pid_alive(pid: int) -> bool:
    """POSIX: signal 0 to probe a PID without touching the process.

    `os.kill(pid, 0)` is safe here, but on Windows it calls TerminateProcess, which
    is why the two paths are separate.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Windows: ask the kernel, never signal it.

    A handle we cannot open means the process is gone; an exit code of 259
    (STILL_ACTIVE) means it is still running, and anything else is its real exit code.
    The handle is always closed.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """Whether a PID belongs to a live process. Never signals a process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def active_recordings(output_dir: Path) -> list[Path]:
    """Lock files whose process is still running; dead ones are cleaned up."""
    locks: list[Path] = []
    if not output_dir.is_dir():
        return locks
    for path in sorted(output_dir.glob(f"{LOCK_PREFIX}*{LOCK_SUFFIX}")):
        pid_text = path.name[len(LOCK_PREFIX) : -len(LOCK_SUFFIX)]
        try:
            pid = int(pid_text)
        except ValueError:
            path.unlink(missing_ok=True)
            continue
        if _pid_alive(pid):
            locks.append(path)
        else:
            path.unlink(missing_ok=True)
    return locks


def acquire_slot(output_dir: Path, max_active: int = MAX_ACTIVE_RECORDINGS) -> Path:
    """Take one of the recording slots, or refuse when they are all in use."""
    output_dir.mkdir(parents=True, exist_ok=True)
    running = active_recordings(output_dir)
    if len(running) >= max_active:
        names = ", ".join(path.name for path in running)
        raise CaptureRefused(
            f"{len(running)} recording(s) already running ({names}); at most {max_active} at once."
        )
    lock = output_dir / f"{LOCK_PREFIX}{os.getpid()}{LOCK_SUFFIX}"
    lock.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
    return lock


def release_slot(lock: Path) -> None:
    lock.unlink(missing_ok=True)


def remux_timestamps(
    video_path: Path,
    *,
    runner=subprocess.run,
    which=shutil.which,
) -> Path | None:
    """Rewrite an fMP4 recording so its timestamps start at zero; keep the original.

    The live encoder counts from its own start, so the file's own duration reads as the
    stream's uptime (a 60 s clip claims 14 hours). `-c copy` remuxes without re-encoding,
    which costs a second on a minute of video. Returns the new path, or None when ffmpeg is
    missing or the remux fails - in which case the original stands and the manifest says so.
    """
    if which("ffmpeg") is None:
        return None
    target = video_path.with_suffix(".timed.mp4")
    try:
        completed = runner(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-c",
                "copy",
                "-reset_timestamps",
                "1",
                "-movflags",
                "+faststart",
                str(target),
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        target.unlink(missing_ok=True)
        return None
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return None
    return target


def resolve_media_playlist(master_url: str, *, fetch=hls.fetch) -> tuple[str, bool]:
    """Follow a master playlist to its media playlist; return (url, is fragmented MP4)."""
    text = fetch(master_url, hls.MAX_PLAYLIST_BYTES, headers=STREAM_HEADERS).decode("utf-8", "replace")
    media_url = hls.parse_master_playlist(text, master_url)
    if media_url != master_url:
        text = fetch(media_url, hls.MAX_PLAYLIST_BYTES, headers=STREAM_HEADERS).decode("utf-8", "replace")
    playlist = hls.parse_media_playlist_detailed(text, media_url)
    return media_url, playlist.is_fragmented_mp4


def open_player_playlist(
    camera_id: str,
    *,
    player_url: str | None = None,
    timeout_seconds: float = PAGE_TIMEOUT_SECONDS,
) -> str:
    """Fallback: load the camera list in headless Chromium and read the playlist it asks for.

    This only works when the camera's card is on the first page of the list, because
    FL511 has no per-camera URL to navigate to and the page does not play anything until
    a card is clicked. `resolve_stream_url` is the default path for that reason; this
    stays for the case where the token chain changes and the page still works.

    Chromium runs headless with its stock user agent: no stealth plugins, no faked
    headers, no fingerprint tricks.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - depends on the optional install
        raise CaptureRefused(
            "playwright is not installed. Run `pip install -r ingestion/requirements-video.txt` "
            "and `playwright install chromium`."
        ) from error

    url = player_url or PLAYER_PAGE
    seen: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on("request", lambda request: seen.append(request.url))
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            refusal = refusal_for_status(response.status if response is not None else None, url)
            if refusal:
                raise CaptureRefused(refusal)

            block = detect_block(page.content())
            if block:
                raise CaptureRefused(block)

            # Nothing plays until the camera's own card is clicked. When the card is not
            # on this page of the list the click finds nothing, and the wait below reports
            # that rather than pretending something was recorded.
            try:
                page.click(f'[data-camera-id="{camera_id}"].showVideo', timeout=10_000)
            except Exception:  # noqa: BLE001 - a direct player URL needs no click
                pass

            # Challenges often appear a moment after the page's markup, so the block
            # check runs again on every pass, not once at domcontentloaded.
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                picked = pick_playlist_url(seen)
                if picked:
                    return picked
                page.wait_for_timeout(500)
                block = detect_block(page.content())
                if block:
                    raise CaptureRefused(block)
            raise CaptureRefused(
                f"No HLS playlist appeared on {url} within {timeout_seconds:.0f}s. "
                "The player may have changed; tell Claude rather than guessing a new endpoint."
            )
        finally:
            browser.close()


def record_fl511_video(
    camera_id: str,
    minutes: float,
    output_dir: Path,
    *,
    player_url: str | None = None,
    page_opener=resolve_stream_url,
    fetch=hls.fetch,
    sleep=time.sleep,
    now=lambda: datetime.now(timezone.utc),
    today: date | None = None,
    clock=time.monotonic,
    remux=remux_timestamps,
) -> Path:
    """Record one FL511 camera for `minutes`, returning the video path.

    The file is `.mp4` when the stream is fragmented MP4 (which FL511's is) and `.ts`
    when it is MPEG-TS, because the playlist decides the container. An fMP4 recording is
    then remuxed once so its timestamps start at zero (`remux_timestamps`, pass `remux=None`
    to skip); the raw file is kept and the manifest names both.
    """
    reason = capture_block_reason(FDOT_VIDEO_CONFIRMED_ON, today)
    if reason:
        raise CaptureRefused(reason)
    if not 0 < minutes <= MAX_MINUTES:
        raise CaptureRefused(f"--minutes must be between 0 and {MAX_MINUTES:g}.")

    output_dir = Path(output_dir)
    lock = acquire_slot(output_dir)
    try:
        camera_dir = output_dir / f"fl511_{camera_id}"
        camera_dir.mkdir(parents=True, exist_ok=True)
        wall_clock = now()

        budget = RefreshBudget(clock=clock)

        opener = page_opener
        if player_url is not None and opener is resolve_stream_url:
            # An explicit page URL only makes sense for the browser path.
            opener = open_player_playlist

        def open_stream() -> str:
            if player_url is None:
                return opener(camera_id)
            return opener(camera_id, player_url=player_url)

        def refresh() -> str:
            if not budget.take():
                raise CaptureRefused(
                    f"The playlist token expired and the refresh budget is spent "
                    f"({budget.used} of {budget.max_refreshes}). Stopping."
                )
            return resolve_media_playlist(open_stream(), fetch=fetch)[0]

        # Resolving can itself meet an expired token, so it draws on the same budget as
        # a mid-recording refresh rather than starting over.
        attempts = 0
        while True:
            try:
                playlist_url, fragmented = resolve_media_playlist(open_stream(), fetch=fetch)
                break
            except hls.PlaylistUnavailable:
                if not budget.take():
                    raise CaptureRefused(
                        f"The playlist token expired and the refresh budget is spent "
                        f"({budget.used} of {budget.max_refreshes}). Stopping."
                    )
                attempts += 1
        video_path = camera_dir / (
            wall_clock.strftime("%Y%m%dT%H%M%SZ") + (".mp4" if fragmented else ".ts")
        )
        recording = hls.record_playlist(
            playlist_url,
            video_path,
            minutes * 60,
            fetch=fetch,
            sleep=sleep,
            now=now,
            refresh=refresh,
            headers=STREAM_HEADERS,
            min_refresh_seconds=REFRESH_MIN_SECONDS,
            max_refreshes=MAX_REFRESHES,
        )
        if recording.segments == 0:
            video_path.unlink(missing_ok=True)
            raise CaptureRefused(
                f"No video was recorded for camera {camera_id} "
                f"(stopped because: {recording.stopped_because})."
            )

        # Step: make the timestamps start at zero. The raw file stays; the timed one is what
        # the manifest points at and what anything playing or dating the clip should use.
        timed = remux(video_path) if remux else None
        playable = timed or video_path

        manifest = {
            "source": SOURCE,
            "camera_id": camera_id,
            "started_at": (recording.started_at or wall_clock).isoformat(timespec="seconds"),
            # as_posix: a manifest written on Windows must read the same on Linux, and
            # str(Path) writes backslashes there.
            "video": playable.relative_to(output_dir).as_posix(),
            "timestamps": (
                "reset after recording (ffmpeg -c copy -reset_timestamps 1)"
                if timed
                else "as recorded: the stream's own clock, not zero"
            ),
            "recorded_seconds": round(recording.recorded_seconds, 3),
            "segments": recording.segments,
            "missed_segments": recording.missed_segments,
            "refreshes": recording.refreshes + attempts,
            "stopped_because": recording.stopped_because,
        }
        if timed:
            manifest["video_raw"] = video_path.relative_to(output_dir).as_posix()
        with (output_dir / "manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=True) + "\n")
        return playable
    finally:
        release_slot(lock)


def describe_capture_state(today: date | None = None) -> str:
    """One line for the CLI: whether capture is off, on, or expired."""
    reason = capture_block_reason(FDOT_VIDEO_CONFIRMED_ON, today)
    if reason:
        return reason
    if EXPIRY_DATE is None:
        return f"FL511 live video capture is ON (confirmed {FDOT_VIDEO_CONFIRMED_ON}); no end date."
    return f"FL511 live video capture is ON (confirmed {FDOT_VIDEO_CONFIRMED_ON}); authorization ends {EXPIRY_DATE}."
