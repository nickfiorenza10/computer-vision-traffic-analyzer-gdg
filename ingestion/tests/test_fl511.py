import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion.fl511 import (
    Camera,
    _camera_from_attributes,
    is_limited_access,
    save_snapshot,
    validate_bbox,
)


def camera_attributes(**overrides):
    attributes = {
        "ID": "1043",
        "DESCRIPT": "Dale Mabry at Hillsborough Ave",
        "COUNTY": "Hillsborough",
        "HIGHWAY": "for City of Tampa cameras",
        "DIRECTION": "N",
        "LATITUDE": 27.995833,
        "LONGITUDE": -82.505,
        "TIMESTAMP": "07/20/2026 8:19:09 PM",
        "IMAGE": "https://images.example.test/camera.jpg",
    }
    attributes.update(overrides)
    return attributes


class CameraParsingTests(unittest.TestCase):
    def test_parse_camera_attributes(self):
        camera = _camera_from_attributes(camera_attributes())
        self.assertEqual(camera.camera_id, "1043")
        self.assertEqual(camera.road, "for City of Tampa cameras")
        self.assertTrue(camera.urban_candidate)

    def test_missing_image_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS image URL"):
            _camera_from_attributes(camera_attributes(IMAGE="http://example.test/camera.jpg"))

    def test_limited_access_roads_and_locations_are_excluded(self):
        for road, description in [
            ("I-275", "at downtown"),
            ("Veterans Expressway", "at Linebaugh Ave"),
            ("SR 60", "I-275 interchange"),
            ("SR 589", "MM 4.3"),
        ]:
            with self.subTest(road=road, description=description):
                self.assertTrue(is_limited_access(road, description))
        self.assertFalse(is_limited_access("Himes Avenue", "at Lemon St"))

    def test_validate_bbox(self):
        validate_bbox((-82.60, 27.80, -82.30, 28.10))
        for bbox in [
            (-82.30, 27.80, -82.60, 28.10),
            (-82.60, 28.10, -82.30, 27.80),
            (-200, 27.80, -82.30, 28.10),
        ]:
            with self.subTest(bbox=bbox), self.assertRaises(ValueError):
                validate_bbox(bbox)


class SnapshotTests(unittest.TestCase):
    def test_writes_jpeg_and_manifest(self):
        camera = _camera_from_attributes(camera_attributes())

        class FakeResponse:
            headers = {"Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"\xff\xd8\xffjpeg-data"

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.fl511.urllib.request.urlopen", return_value=FakeResponse()
        ):
            output_dir = Path(temp_dir)
            snapshot = save_snapshot(camera, output_dir)
            self.assertEqual(snapshot.read_bytes(), b"\xff\xd8\xffjpeg-data")
            manifest = json.loads((output_dir / "manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(manifest["camera_id"], "1043")
            self.assertEqual(manifest["snapshot"], snapshot.relative_to(output_dir).as_posix())
            self.assertIn("captured_at", manifest)

    def test_rejects_non_image_response(self):
        camera = _camera_from_attributes(camera_attributes())

        class FakeResponse:
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b"<html>not an image</html>"

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "ingestion.fl511.urllib.request.urlopen", return_value=FakeResponse()
        ):
            with self.assertRaisesRegex(RuntimeError, "not an image"):
                save_snapshot(camera, Path(temp_dir))


if __name__ == "__main__":
    unittest.main()
