import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_source import (  # noqa: E402
    HfSpec,
    parse_hf_spec,
    resolve_model,
    resolve_model_cli,
)

HF_VALUE = "hf:rycebag/traffic-weights/yolo26m.pt"


class FakeDownload:
    """Stands in for huggingface_hub.hf_hub_download so the tests stay offline."""

    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return f"/cache/{kwargs['repo_id']}/{kwargs['filename']}"


class ParseSpecTests(unittest.TestCase):
    def test_plain_values_are_not_hf_specs(self):
        for value in [
            "yolo26m.pt",
            "yolo26s.pt",
            "./weights/yolo26m.pt",
            "/home/me/models/yolo26m.pt",
            "C:\\models\\yolo26m.pt",
            "",
        ]:
            with self.subTest(value=value):
                self.assertIsNone(parse_hf_spec(value))

    def test_parses_owner_repo_and_filename(self):
        spec = parse_hf_spec(HF_VALUE)
        self.assertEqual(spec, HfSpec(owner="rycebag", repo="traffic-weights", filename="yolo26m.pt"))
        assert spec is not None
        self.assertEqual(spec.repo_id, "rycebag/traffic-weights")
        self.assertEqual(spec.as_model_value(), HF_VALUE)

    def test_filename_may_contain_subdirectories(self):
        spec = parse_hf_spec("hf:owner/repo/weights/best/yolo26m.pt")
        assert spec is not None
        self.assertEqual(spec.filename, "weights/best/yolo26m.pt")
        self.assertEqual(spec.repo_id, "owner/repo")

    def test_malformed_values_raise_with_a_usable_message(self):
        for value in [
            "hf:",
            "hf:owner",
            "hf:owner/repo",
            "hf:owner/repo/",
            "hf:/repo/model.pt",
            "hf:owner//model.pt",
            "hf:owner/repo/../model.pt",
            "hf:owner/repo/./model.pt",
        ]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    parse_hf_spec(value)
                self.assertIn("hf:<owner>/<repo>/<filename>", str(caught.exception))


class ResolveModelTests(unittest.TestCase):
    def test_plain_values_pass_through_untouched(self):
        download = FakeDownload()
        for value in ["yolo26m.pt", "./weights/best.pt", "/abs/yolo26m.pt"]:
            with self.subTest(value=value):
                self.assertEqual(resolve_model(value, download=download, environ={}), value)
        self.assertEqual(download.calls, [])

    def test_downloads_the_spec_and_returns_the_local_path(self):
        download = FakeDownload()
        path = resolve_model(HF_VALUE, download=download, environ={})
        self.assertEqual(path, "/cache/rycebag/traffic-weights/yolo26m.pt")
        self.assertEqual(
            download.calls,
            [{"repo_id": "rycebag/traffic-weights", "filename": "yolo26m.pt"}],
        )

    def test_uses_hf_token_from_the_environment(self):
        download = FakeDownload()
        resolve_model(HF_VALUE, download=download, environ={"HF_TOKEN": "hf_secret"})
        self.assertEqual(download.calls[0]["token"], "hf_secret")

    def test_empty_token_is_not_sent(self):
        download = FakeDownload()
        resolve_model(HF_VALUE, download=download, environ={"HF_TOKEN": ""})
        self.assertNotIn("token", download.calls[0])

    def test_explicit_token_wins_over_the_environment(self):
        download = FakeDownload()
        resolve_model(HF_VALUE, token="explicit", download=download, environ={"HF_TOKEN": "env"})
        self.assertEqual(download.calls[0]["token"], "explicit")

    def test_failed_download_is_reported_with_the_model(self):
        download = FakeDownload(error=OSError("404 not found"))
        with self.assertRaises(RuntimeError) as caught:
            resolve_model(HF_VALUE, download=download, environ={})
        message = str(caught.exception)
        self.assertIn(HF_VALUE, message)
        self.assertIn("404 not found", message)


class CliTests(unittest.TestCase):
    def test_plain_value_is_returned(self):
        self.assertEqual(resolve_model_cli("yolo26n.pt", "track_count.py", download=FakeDownload()), "yolo26n.pt")

    def test_bad_value_exits_with_the_program_name(self):
        with self.assertRaises(SystemExit) as caught:
            resolve_model_cli("hf:owner/repo", "detect.py", download=FakeDownload())
        self.assertIn("detect.py", str(caught.exception))

    def test_hf_value_is_resolved_without_the_network(self):
        download = FakeDownload()
        path = resolve_model_cli(HF_VALUE, "auto_lanes.py", download=download)
        self.assertEqual(path, "/cache/rycebag/traffic-weights/yolo26m.pt")
        self.assertEqual(len(download.calls), 1)


if __name__ == "__main__":
    unittest.main()
