"""Hugging Face Space worker: turn a live Caltrans camera into rows in Postgres.

A small FastAPI service. One job at a time, on demand, and only for cameras that
already have a committed lanes file: the worker records a few minutes of the
camera's HLS stream, runs the pipeline over it, and imports the events. It never
learns lanes by itself, because an unattended `--auto-lanes` run can commit a lane
layout nobody checked, and it never touches FL511.

    GET  /health          liveness plus whether the environment is complete
    POST /jobs/record     {"camera_id": "D3-1", "minutes": 5}  Authorization: Bearer <token>
    GET  /jobs/latest     the last job's status and log tail

Environment, all read at startup:

    DATABASE_URL    Postgres URL for the import (a writing login, not the API's read-only one)
    WORKER_TOKEN    the bearer token every /jobs call must present
    DATA_DIR        where recordings and outputs go (default: /data)
    WORKER_MODEL    YOLO weights for the worker (default: yolo26n.pt)
    WORKER_IMGSZ    model input size for the worker (default: 640)
    WORKER_DEVICE   cpu (default) or a GPU index such as 0
    WORKER_TEST_CLIP  test only: copy this local clip instead of recording a camera

The worker sets its own model and image size rather than changing the pipeline's
defaults, which were measured on a GPU. On Space CPU hardware a smaller model and
input size is the difference between minutes and an hour per recording.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # imported as the package's module (uvicorn worker.app:app)
    from . import live as live_analysis
    from . import sweep as sweep_module
    from . import sweep_store
except ImportError:  # imported as a plain module from the worker directory
    import live as live_analysis
    import sweep as sweep_module
    import sweep_store

MAX_MINUTES = 10.0
# Caltrans CCTV camera ids: district 1-12, then a 1-5 digit camera number. Anything
# else would reach a path, a source id and a command line, so it is refused up front.
CAMERA_ID_PATTERN = r"^D([1-9]|1[0-2])-\d{1,5}$"
DEFAULT_MODEL = "yolo26n.pt"
DEFAULT_IMGSZ = 640
# CPU by default (the Docker image has no GPU); worker/run_local.ps1 sets "0" for a GPU.
DEFAULT_DEVICE = "cpu"
LOG_TAIL = 40
REPO_ROOT = Path(__file__).resolve().parents[1]

Status = Literal["running", "succeeded", "failed"]


class JobRequest(BaseModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    minutes: float = Field(default=5, gt=0)


# Live sessions also take Florida cameras, by their FL511 map id: "fl511-2849".
LIVE_CAMERA_ID_PATTERN = r"^(D([1-9]|1[0-2])-\d{1,5}|fl511-\d{1,7})$"


class LiveRequest(BaseModel):
    """A live session asks for the bare Caltrans id (D3-41) or a Florida one (fl511-2849)."""

    camera_id: str = Field(pattern=LIVE_CAMERA_ID_PATTERN)


@dataclass
class Job:
    job_id: str
    camera_id: str
    minutes: float
    started_at: str
    status: Status = "running"
    finished_at: str | None = None
    sources: int = 0
    step: str = "starting"
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log.append(f"{stamp} {line}")
        del self.log[:-200]

    def as_dict(self, include_log: bool = True) -> dict:
        payload = {
            "job_id": self.job_id,
            "camera_id": self.camera_id,
            "minutes": self.minutes,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "step": self.step,
            "sources": self.sources,
            "error": self.error,
        }
        if include_log:
            payload["log_tail"] = "\n".join(self.log[-LOG_TAIL:])
        return payload


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    worker_token: str | None
    data_dir: Path
    model: str = DEFAULT_MODEL
    imgsz: int = DEFAULT_IMGSZ
    device: str = DEFAULT_DEVICE
    test_clip: str | None = None
    repo_root: Path = REPO_ROOT
    sweep_districts: tuple[int, ...] = ()

    @property
    def problems(self) -> list[str]:
        issues = []
        if not self.database_url:
            issues.append("DATABASE_URL is not set: jobs cannot import events.")
        if not self.worker_token:
            issues.append("WORKER_TOKEN is not set: every job request would be refused.")
        return issues

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("DATABASE_URL") or None,
            worker_token=os.environ.get("WORKER_TOKEN") or None,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            model=os.environ.get("WORKER_MODEL", DEFAULT_MODEL),
            imgsz=int(os.environ.get("WORKER_IMGSZ", DEFAULT_IMGSZ)),
            device=os.environ.get("WORKER_DEVICE", DEFAULT_DEVICE),
            test_clip=os.environ.get("WORKER_TEST_CLIP") or None,
            sweep_districts=tuple(
                int(part)
                for part in os.environ.get("WORKER_SWEEP_DISTRICTS", "").replace(" ", "").split(",")
                if part
            ),
        )


class JobBusy(RuntimeError):
    """A job is already running; the worker does them one at a time."""


class JobFailed(RuntimeError):
    """A step of the job failed; the message is shown to the caller."""


def check_camera_id(camera_id: str) -> None:
    """Refuse anything but a Caltrans camera id, before it reaches a path or a command.

    FastAPI already rejects a bad body field with 422 through JobRequest; this covers
    the same rule for direct callers of Worker.start().
    """
    if not camera_id_is_valid(camera_id):
        raise HTTPException(
            status_code=422,
            detail=(
                "camera_id must look like D3-1 (district 1-12, then 1-5 digits); "
                f"got {camera_id!r}"
            ),
        )


def lanes_path(camera_id: str, repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "pipeline" / f"lanes_caltrans_{camera_id}.json"


def _run(command: list[str], *, env: dict[str, str], job: Job, timeout: float, cwd: Path = REPO_ROOT) -> str:
    """Run a pipeline command, keeping a short tail of its output in the job log."""
    job.note("$ " + " ".join(command))
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=str(cwd),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    for line in output.splitlines()[-8:]:
        job.note(line)
    if completed.returncode != 0:
        # A view check that stops the run says why in a line of its own; passing that on
        # is more useful than "exited 1".
        for line in output.splitlines():
            if line.strip().startswith("camera view changed:"):
                raise JobFailed(line.strip())
        raise JobFailed(f"{Path(command[2] if len(command) > 2 else command[0]).name} exited {completed.returncode}")
    return completed.stdout


def record_camera(settings: Settings, camera_id: str, minutes: float, job: Job) -> Path:
    """Record the camera, or copy a local clip when WORKER_TEST_CLIP is set."""
    if settings.test_clip:
        source = Path(settings.test_clip)
        if not source.exists():
            raise JobFailed(f"WORKER_TEST_CLIP points at {source}, which does not exist")
        target_dir = settings.data_dir / f"caltrans_{camera_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)
        target = target_dir / (started.strftime("%Y%m%dT%H%M%SZ") + source.suffix)
        target.write_bytes(source.read_bytes())
        manifest = {
            "started_at": started.isoformat(timespec="seconds"),
            "video": target.relative_to(settings.data_dir).as_posix(),
            "recorded_seconds": minutes * 60,
            "segments": 0,
            "missed_segments": 0,
            "source": "test-clip",
            "camera_id": camera_id,
        }
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with (settings.data_dir / "manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=True) + "\n")
        job.note(f"test recorder: copied {source.name} instead of recording {camera_id}")
        return target

    stdout = _run(
        [
            sys.executable,
            "-m",
            "ingestion",
            "caltrans-record",
            "--camera-id",
            camera_id,
            "--minutes",
            str(minutes),
            "--output",
            str(settings.data_dir),
        ],
        env={**os.environ},
        job=job,
        timeout=minutes * 60 + 180,
        cwd=settings.repo_root,
    )
    for line in stdout.splitlines():
        if line.startswith("Saved video:"):
            return Path(line.split(":", 1)[1].strip())
    raise JobFailed("the recorder did not report a saved video")


def manifest_start(settings: Settings, video: Path) -> str:
    """`started_at` for the recording, from the manifest the recorder wrote."""
    manifest_path = settings.data_dir / "manifest.jsonl"
    if not manifest_path.exists():
        raise JobFailed("the recorder wrote no manifest, so the clip has no start time")
    wanted = video.name
    for line in reversed(manifest_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A manifest written on Windows carries backslashes; Path() on Linux treats them as
        # part of the file name, so normalise before comparing.
        recorded = str(entry.get("video", "")).replace("\\", "/")
        if Path(recorded).name == wanted and entry.get("started_at"):
            return str(entry["started_at"])
    raise JobFailed("no manifest line for this recording")


def track_video(settings: Settings, video: Path, lanes: Path, start: str, job: Job) -> Path:
    _run(
        [
            sys.executable,
            "pipeline/track_count.py",
            str(video),
            "--lanes",
            str(lanes),
            "--start",
            start,
            "--device",
            settings.device,
            "--model",
            settings.model,
            "--imgsz",
            str(settings.imgsz),
            "--view-check",
            "strict",
        ],
        env={**os.environ},
        job=job,
        timeout=60 * 60,
        cwd=settings.repo_root,
    )
    events = settings.repo_root / "data" / "output" / f"{video.stem}_events.csv"
    if not events.exists():
        raise JobFailed(f"the pipeline wrote no {events.name}")
    return events


def load_events(settings: Settings, events: Path, source_id: str, camera_id: str, job: Job) -> int:
    stdout = _run(
        [
            sys.executable,
            "-m",
            "database.load_events",
            str(events),
            "--source-id",
            source_id,
            "--camera-id",
            camera_id,
        ],
        env={**os.environ, "DATABASE_URL": settings.database_url or ""},
        job=job,
        timeout=300,
        cwd=settings.repo_root,
    )
    for line in stdout.splitlines():
        if line.startswith(("Imported:", "Already imported:")):
            digits = "".join(ch for ch in line.split(":", 1)[1].split("events")[0] if ch.isdigit())
            return int(digits or 0)
    return 0


def camera_id_is_valid(camera_id: str) -> bool:
    return bool(re.fullmatch(CAMERA_ID_PATTERN, camera_id))


def source_id_for(camera_id: str, start: str) -> str:
    """`caltrans-<camera>-<UTC start>`, with the timestamp reduced to a sortable stamp."""
    stamp = start.replace("-", "").replace(":", "").split("+")[0].split("Z")[0]
    stamp = stamp.replace(".", "")
    return f"caltrans-{camera_id}-{stamp}"


class Worker:
    """Holds the single job slot and the last job's state."""

    def __init__(
        self,
        settings: Settings,
        *,
        recorder: Callable[[Settings, str, float, Job], Path] = record_camera,
        tracker: Callable[[Settings, Path, Path, str, Job], Path] = track_video,
        loader: Callable[[Settings, Path, str, str, Job], int] = load_events,
        start_time: Callable[[Settings, Path], str] = manifest_start,
        live: "live_analysis.LiveManager | None" = None,
        sweep: "sweep_module.Sweeper | None" = None,
    ):
        self.settings = settings
        self.sweep = sweep
        if self.sweep is None and settings.sweep_districts and settings.database_url:
            self.sweep = sweep_module.Sweeper(
                store=sweep_store.PostgresStore(settings.database_url),
                districts=settings.sweep_districts,
                model=settings.model,
                imgsz=settings.imgsz,
                device=settings.device,
            )
        self.live = live or live_analysis.LiveManager(
            camera_lookup=live_analysis.LiveManager.default_lookup,
            device=settings.device,
            model=settings.model,
            imgsz=settings.imgsz,
        )
        self._recorder = recorder
        self._tracker = tracker
        self._loader = loader
        self._start_time = start_time
        self._lock = threading.Lock()
        self._latest: Job | None = None
        self._thread: threading.Thread | None = None

    @property
    def latest(self) -> Job | None:
        return self._latest

    def start(self, camera_id: str, minutes: float) -> Job:
        problems = self.settings.problems
        if problems:
            raise HTTPException(status_code=503, detail=" ".join(problems))
        check_camera_id(camera_id)
        if minutes > MAX_MINUTES:
            raise HTTPException(
                status_code=400, detail=f"minutes must be at most {MAX_MINUTES:g}; {minutes:g} was requested."
            )
        if not lanes_path(camera_id, self.settings.repo_root).exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No lanes file for camera {camera_id}. Ask Claude to record it and approve "
                    f"pipeline/lanes_caltrans_{camera_id}.json; the worker never runs --auto-lanes."
                ),
            )
        with self._lock:
            if self._latest is not None and self._latest.status == "running":
                raise HTTPException(
                    status_code=409,
                    detail="A recording is already running. One job at a time.",
                )
            job = Job(
                job_id=secrets.token_hex(6),
                camera_id=camera_id,
                minutes=minutes,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._latest = job
            self._thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            self._thread.start()
        return job

    def _run(self, job: Job) -> None:
        try:
            job.step = "recording"
            video = self._recorder(self.settings, job.camera_id, job.minutes, job)

            job.step = "reading the manifest"
            start = self._start_time(self.settings, video)

            job.step = "tracking"
            lanes = lanes_path(job.camera_id, self.settings.repo_root)
            events = self._tracker(self.settings, video, lanes, start, job)

            job.step = "importing"
            job.sources = self._loader(
                self.settings,
                events,
                source_id_for(job.camera_id, start),
                f"caltrans-{job.camera_id}",
                job,
            )
            job.step = "done"
            job.status = "succeeded"
        except JobFailed as error:
            job.status = "failed"
            job.error = str(error)
        except Exception as error:  # noqa: BLE001 - surfaced through /jobs/latest
            job.status = "failed"
            job.error = f"{type(error).__name__}: {error}"
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_app(worker: Worker | None = None) -> FastAPI:
    settings = worker.settings if worker else Settings.from_env()
    state = worker or Worker(settings)
    app = FastAPI(title="traffic-worker", version="0.1.0")

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = state.settings.worker_token
        if not expected:
            raise HTTPException(status_code=503, detail="WORKER_TOKEN is not set on the worker.")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Send Authorization: Bearer <token>.")
        offered = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(offered, expected):
            raise HTTPException(status_code=401, detail="That token is not the worker's token.")

    @app.get("/health")
    def health() -> dict:
        # /health is the one route without a token and it is reachable through the tunnel,
        # so it says only whether things are busy: no session id (which is enough to watch or
        # stop somebody else's session) and no last_error (a database error can carry a host).
        current = state.live.session
        live_summary = {"busy": bool(current is not None and not current.is_finished())}
        sweep_summary = None
        if state.sweep is not None:
            sweep = state.sweep.status_dict()
            sweep_summary = {"running": sweep["running"], "next_run": sweep["next_run_at"]}
        return {
            "status": "ok" if not state.settings.problems else "misconfigured",
            "problems": state.settings.problems,
            "job": state.latest.as_dict(include_log=False) if state.latest else None,
            "live": live_summary,
            "sweep": sweep_summary,
        }

    @app.post("/jobs/record", status_code=202, dependencies=[Depends(require_token)])
    def record(request: JobRequest) -> dict:
        job = state.start(request.camera_id, request.minutes)
        return {"job_id": job.job_id, "status": job.status, "camera_id": job.camera_id}

    @app.get("/jobs/latest", dependencies=[Depends(require_token)])
    def latest() -> dict:
        if state.latest is None:
            raise HTTPException(status_code=404, detail="No job has run yet.")
        return state.latest.as_dict()

    # -- live analysis (T16) ---------------------------------------------
    @app.post("/live/start", status_code=202, dependencies=[Depends(require_token)])
    def live_start(request: LiveRequest) -> dict:
        try:
            return state.live.start(request.camera_id)
        except live_analysis.LiveBusy as busy:
            return Response(
                status_code=409,
                media_type="application/json",
                content=json.dumps(
                    {
                        "error": "another live analysis is running",
                        "retry_after_s": busy.retry_after,
                    }
                ),
            )
        except live_analysis.LiveUnavailable as error:
            # 503 with a code the panel can branch on: a dead stream and an unreachable
            # worker are both 503, and the panel must not offer "try again" for one and
            # not the other.
            return JSONResponse(
                status_code=503,
                content={"error": str(error), "error_code": error.error_code},
            )
        except Exception as error:  # noqa: BLE001 - unknown camera, no stream, no model
            return JSONResponse(
                status_code=503,
                content={
                    "error": "That camera cannot be analysed right now.",
                    "error_code": "unavailable",
                },
            )

    @app.get("/live/offline", dependencies=[Depends(require_token)])
    def live_offline() -> dict:
        # `cameras` is the list the notes specify (plain ids); `details` adds the reason and
        # how long ago, for anything that wants to say more than "not this one".
        return {
            "cameras": state.live.offline_cameras(),
            "details": state.live.offline_details(),
        }

    @app.get("/live/{session_id}", dependencies=[Depends(require_token)])
    def live_status(session_id: str) -> dict:
        status = state.live.get(session_id)
        if status is None:
            raise HTTPException(status_code=404, detail="No such live session.")
        return status

    @app.get("/live/{session_id}/frame.jpg", dependencies=[Depends(require_token)])
    def live_frame(session_id: str) -> Response:
        jpeg = state.live.frame(session_id)
        if jpeg is None:
            raise HTTPException(status_code=404, detail="No frame yet.")
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.delete("/live/{session_id}", dependencies=[Depends(require_token)])
    def live_stop(session_id: str) -> dict:
        if not state.live.stop(session_id):
            raise HTTPException(status_code=404, detail="No such live session.")
        return {"stopped": True, "session_id": session_id}

    # -- the 30-minute sweep (T15) ----------------------------------------
    @app.get("/sweep/status", dependencies=[Depends(require_token)])
    def sweep_status() -> dict:
        if state.sweep is None:
            raise HTTPException(
                status_code=503,
                detail="The sweep is not configured: set WORKER_SWEEP_DISTRICTS, e.g. 3.",
            )
        return state.sweep.status_dict()

    if state.sweep is not None:
        state.sweep.start()

    return app


app = create_app()
