import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from worker.app import (  # noqa: E402
    Job,
    JobFailed,
    Settings,
    Worker,
    _run,
    create_app,
    source_id_for,
)

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def make_worker(tmp: Path, *, recorder=None, tracker=None, loader=None, token=TOKEN, camera="D3-1"):
    settings = Settings(
        database_url="postgresql://traffic:traffic_dev@127.0.0.1:5432/traffic",
        worker_token=token,
        data_dir=tmp / "data",
        test_clip=None,
        repo_root=tmp,
    )
    return Worker(
        settings,
        recorder=recorder or (lambda s, camera_id, minutes, job: tmp / "clip.ts"),
        tracker=tracker or (lambda s, video, lanes, start, job: tmp / "events.csv"),
        loader=loader or (lambda s, events, source, camera, job: 12),
        start_time=lambda s, video: "2026-10-29T14:03:00+00:00",
    )


class AuthTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pipeline").mkdir()
        (self.tmp / "pipeline" / "lanes_caltrans_D3-1.json").write_text("{}")
        self.worker = make_worker(self.tmp)
        self.client = TestClient(create_app(self.worker))

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_token_is_401(self):
        response = self.client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 1})
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_is_401(self):
        response = self.client.post(
            "/jobs/record", json={"camera_id": "D3-1", "minutes": 1}, headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(response.status_code, 401)

    def test_latest_also_needs_the_token(self):
        self.assertEqual(self.client.get("/jobs/latest").status_code, 401)

    def test_no_configured_token_is_503(self):
        worker = make_worker(self.tmp, token=None)
        client = TestClient(create_app(worker))
        response = client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 1}, headers=HEADERS)
        self.assertEqual(response.status_code, 503)

    def test_health_is_open(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pipeline").mkdir()
        self.worker = make_worker(self.tmp)
        self.client = TestClient(create_app(self.worker))

    def tearDown(self):
        self._tmp.cleanup()

    def test_minutes_above_ten_is_rejected(self):
        (self.tmp / "pipeline" / "lanes_caltrans_D3-1.json").write_text("{}")
        response = self.client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 11}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 10", response.json()["detail"])

    def test_a_missing_lanes_file_fails_clearly(self):
        response = self.client.post("/jobs/record", json={"camera_id": "D9-9", "minutes": 1}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("lanes_caltrans_D9-9.json", detail)
        self.assertIn("auto-lanes", detail)

    def test_latest_before_any_job_is_404(self):
        self.assertEqual(self.client.get("/jobs/latest", headers=HEADERS).status_code, 404)

    def test_a_path_traversal_camera_id_is_422(self):
        for bad in ("../x", "../../etc/passwd", "D3-1/../../x"):
            response = self.client.post("/jobs/record", json={"camera_id": bad, "minutes": 1}, headers=HEADERS)
            self.assertEqual(response.status_code, 422, bad)

    def test_an_empty_camera_id_is_422(self):
        for bad in ("", "   "):
            response = self.client.post("/jobs/record", json={"camera_id": bad, "minutes": 1}, headers=HEADERS)
            self.assertEqual(response.status_code, 422, repr(bad))

    def test_a_district_outside_one_to_twelve_is_422(self):
        for bad in ("D13-1", "D0-1", "D-1", "D3-", "D3-123456"):
            response = self.client.post("/jobs/record", json={"camera_id": bad, "minutes": 1}, headers=HEADERS)
            self.assertEqual(response.status_code, 422, bad)

    def test_the_whole_valid_id_range_is_accepted(self):
        # The pattern must accept every district and 1-5 digits, not just D3-1.
        for good in ("D1-1", "D9-42", "D10-12345", "D12-7"):
            (self.tmp / "pipeline" / f"lanes_caltrans_{good}.json").write_text("{}")
            response = self.client.post(
                "/jobs/record", json={"camera_id": good, "minutes": 0.1}, headers=HEADERS
            )
            self.assertEqual(response.status_code, 202, good)
            for _ in range(200):
                job = self.worker.latest
                if job is None or job.status != "running":
                    break
                time.sleep(0.05)

    def test_a_bad_id_is_refused_before_the_lanes_file_is_read(self):
        # A traversal id must fail on the pattern, never on a path that happens to
        # exist outside the repo.
        response = self.client.post("/jobs/record", json={"camera_id": "../pipeline", "minutes": 1}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("lanes_caltrans", str(response.json()))


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pipeline").mkdir()
        (self.tmp / "pipeline" / "lanes_caltrans_D3-1.json").write_text("{}")
        self.release = threading.Event()
        self.started = threading.Event()

    def tearDown(self):
        self.release.set()
        self._tmp.cleanup()

    def slow_recorder(self, settings, camera_id, minutes, job):
        self.started.set()
        self.release.wait(timeout=10)
        return self.tmp / "clip.ts"

    def test_second_job_gets_409(self):
        worker = make_worker(self.tmp, recorder=self.slow_recorder)
        client = TestClient(create_app(worker))

        first = client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 1}, headers=HEADERS)
        self.assertEqual(first.status_code, 202)
        self.assertTrue(self.started.wait(timeout=5), "the first job never started")

        second = client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 1}, headers=HEADERS)
        self.assertEqual(second.status_code, 409)
        self.assertIn("one job at a time", second.json()["detail"].lower())

        self.release.set()
        for _ in range(100):
            if worker.latest and worker.latest.status != "running":
                break
            time.sleep(0.02)
        self.assertEqual(worker.latest.status, "succeeded")


class JobPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pipeline").mkdir()
        (self.tmp / "pipeline" / "lanes_caltrans_D3-1.json").write_text("{}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_successful_job_records_the_steps_and_rows(self):
        calls = {}

        def recorder(settings, camera_id, minutes, job):
            calls["record"] = (camera_id, minutes)
            job.note("recording")
            return self.tmp / "clip.ts"

        def tracker(settings, video, lanes, start, job):
            calls["track"] = (Path(video).name, Path(lanes).name, start)
            return self.tmp / "events.csv"

        def loader(settings, events, source, camera, job):
            calls["load"] = (source, camera)
            return 42

        worker = make_worker(self.tmp, recorder=recorder, tracker=tracker, loader=loader)
        client = TestClient(create_app(worker))

        response = client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 2}, headers=HEADERS)
        self.assertEqual(response.status_code, 202)

        for _ in range(100):
            if worker.latest and worker.latest.status != "running":
                break
            time.sleep(0.02)

        job = worker.latest
        self.assertEqual(job.status, "succeeded", job.error)
        self.assertEqual(calls["record"], ("D3-1", 2))
        self.assertEqual(calls["track"][1], "lanes_caltrans_D3-1.json")
        self.assertEqual(calls["track"][2], "2026-10-29T14:03:00+00:00")
        self.assertEqual(calls["load"], ("caltrans-D3-1-20261029T140300", "caltrans-D3-1"))
        self.assertEqual(job.sources, 42)

        latest = client.get("/jobs/latest", headers=HEADERS).json()
        self.assertEqual(latest["status"], "succeeded")
        self.assertIn("log_tail", latest)
        self.assertEqual(latest["step"], "done")

    def test_a_failing_step_marks_the_job_failed_without_leaking_the_database_url(self):
        def recorder(settings, camera_id, minutes, job):
            raise RuntimeError("camera D3-1 stream ended")

        worker = make_worker(self.tmp, recorder=recorder)
        client = TestClient(create_app(worker))
        client.post("/jobs/record", json={"camera_id": "D3-1", "minutes": 1}, headers=HEADERS)

        for _ in range(100):
            if worker.latest and worker.latest.status != "running":
                break
            time.sleep(0.02)

        job = worker.latest
        self.assertEqual(job.status, "failed")
        self.assertIn("stream ended", job.error or "")
        self.assertNotIn("traffic_dev", (job.error or "") + "\n".join(job.log))


class ViewCheckMessageTests(unittest.TestCase):
    """A moved camera must reach the job as an instruction, not as an exit code."""

    def _job(self) -> Job:
        return Job(job_id="j1", camera_id="D3-1", minutes=1, started_at="2026-10-29T14:03:00+00:00")

    def test_a_moved_view_fails_the_job_with_the_pipeline_s_own_words(self):
        job = self._job()
        command = [
            sys.executable,
            "-c",
            "print('camera view check: moved (shifted 12.0%)'); "
            "raise SystemExit('camera view changed: re-learn the lanes (shifted 12.0%).')",
        ]
        with self.assertRaises(JobFailed) as caught:
            _run(command, env={}, job=job, timeout=30)
        self.assertTrue(str(caught.exception).startswith("camera view changed: re-learn the lanes"))
        self.assertIn("camera view check: moved", "\n".join(job.log))

    def test_any_other_failure_keeps_the_exit_code_message(self):
        job = self._job()
        command = [sys.executable, "-c", "raise SystemExit(3)"]
        with self.assertRaises(JobFailed) as caught:
            _run(command, env={}, job=job, timeout=30)
        self.assertIn("exited 3", str(caught.exception))


class ManifestLookupTests(unittest.TestCase):
    """A manifest written on Michael's PC has backslashes; the worker must still find it."""

    def _settings(self, directory: Path) -> Settings:
        return Settings(
            database_url="postgresql://traffic:***@127.0.0.1:5432/traffic",
            worker_token=TOKEN,
            data_dir=directory,
            repo_root=directory,
        )

    def test_a_windows_style_video_path_is_matched(self):
        import json
        import tempfile

        from worker.app import manifest_start

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.jsonl").write_text(
                json.dumps(
                    {
                        "camera_id": "D3-1",
                        "video": "caltrans_D3-1\\20261029T083000Z.ts",
                        "started_at": "2026-10-29T08:30:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            start = manifest_start(self._settings(root), root / "20261029T083000Z.ts")
        self.assertEqual(start, "2026-10-29T08:30:00+00:00")

    def test_a_posix_video_path_is_matched_too(self):
        import json
        import tempfile

        from worker.app import manifest_start

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.jsonl").write_text(
                json.dumps(
                    {
                        "video": "caltrans_D3-1/20261029T083000Z.ts",
                        "started_at": "2026-10-29T08:31:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            start = manifest_start(self._settings(root), root / "20261029T083000Z.ts")
        self.assertEqual(start, "2026-10-29T08:31:00+00:00")


class SourceIdTests(unittest.TestCase):
    def test_source_id_is_sortable_and_namespaced(self):
        self.assertEqual(
            source_id_for("D3-1", "2026-10-29T14:03:05+00:00"),
            "caltrans-D3-1-20261029T140305",
        )

    def test_a_zulu_timestamp_works_too(self):
        self.assertEqual(source_id_for("D7-42", "2026-10-29T14:03:00Z"), "caltrans-D7-42-20261029T140300")


if __name__ == "__main__":
    unittest.main()
