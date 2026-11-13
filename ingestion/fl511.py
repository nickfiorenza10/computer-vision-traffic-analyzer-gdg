"""Read Tampa-area camera metadata and capture FL511 image snapshots."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

CAMERA_LAYER_URL = (
    "https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services/"
    "FL511_Traffic_Cameras/FeatureServer/0"
)
TAMPA_BBOX = (-82.60, 27.80, -82.30, 28.10)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FIELDS = "ID,DESCRIPT,COUNTY,HIGHWAY,DIRECTION,LATITUDE,LONGITUDE,TIMESTAMP,IMAGE"
LIMITED_ACCESS_RE = re.compile(
    r"(^|\b)(i[- ]?\d+|interstate|expressway|turnpike|veterans|selmon)(\b|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Camera:
    camera_id: str
    description: str
    county: str
    road: str
    direction: str
    latitude: float
    longitude: float
    provider_timestamp: str
    image_url: str

    @property
    def urban_candidate(self) -> bool:
        return not is_limited_access(self.road, self.description)


def is_limited_access(road: str, description: str) -> bool:
    text = f"{road} {description}"
    return bool(LIMITED_ACCESS_RE.search(text) or re.search(r"\b(ramp|interchange|mm\s*\d)", text, re.I))


def validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ValueError("Bounding box must be west,south,east,north.")
    west, south, east, north = bbox
    if not all(-180 <= value <= 180 for value in (west, east)) or not all(
        -90 <= value <= 90 for value in (south, north)
    ):
        raise ValueError("Bounding box coordinates are outside valid longitude/latitude ranges.")
    if west >= east or south >= north:
        raise ValueError("Bounding box must have west < east and south < north.")


def _request_json(url: str, params: dict[str, str]) -> dict:
    request_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(request_url, headers={"User-Agent": "TrafficAnalyzer/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"FL511 request failed: {exc}") from exc
    if "error" in result:
        error = result["error"]
        raise RuntimeError(f"FL511 returned error {error.get('code')}: {error.get('message')}")
    return result


def _camera_from_attributes(attributes: dict) -> Camera:
    camera_id = str(attributes.get("ID") or "").strip()
    image_url = str(attributes.get("IMAGE") or "").strip()
    if not camera_id:
        raise ValueError("FL511 camera record is missing its ID.")
    if not image_url.startswith("https://"):
        raise ValueError(f"Camera {camera_id} has no HTTPS image URL.")
    try:
        latitude = float(attributes["LATITUDE"])
        longitude = float(attributes["LONGITUDE"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Camera {camera_id} has invalid coordinates.") from exc
    return Camera(
        camera_id=camera_id,
        description=str(attributes.get("DESCRIPT") or "").strip(),
        county=str(attributes.get("COUNTY") or "").strip(),
        road=str(attributes.get("HIGHWAY") or "").strip(),
        direction=str(attributes.get("DIRECTION") or "").strip(),
        latitude=latitude,
        longitude=longitude,
        provider_timestamp=str(attributes.get("TIMESTAMP") or "").strip(),
        image_url=image_url,
    )


def list_cameras(
    bbox: tuple[float, float, float, float] = TAMPA_BBOX,
    include_highways: bool = False,
) -> list[Camera]:
    validate_bbox(bbox)
    west, south, east, north = bbox
    cameras: list[Camera] = []
    offset = 0
    while True:
        result = _request_json(
            f"{CAMERA_LAYER_URL}/query",
            {
                "where": "COUNTY='Hillsborough'",
                "geometry": f"{west},{south},{east},{north}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": FIELDS,
                "returnGeometry": "false",
                "resultRecordCount": "2000",
                "resultOffset": str(offset),
                "f": "json",
            },
        )
        features = result.get("features")
        if not isinstance(features, list):
            raise RuntimeError("FL511 response did not contain a camera feature list.")
        cameras.extend(_camera_from_attributes(feature.get("attributes", {})) for feature in features)
        offset += len(features)
        if not result.get("exceededTransferLimit"):
            break
        if not features:
            raise RuntimeError("FL511 indicated more camera records but returned an empty page.")

    if not include_highways:
        cameras = [camera for camera in cameras if camera.urban_candidate]
    return sorted(cameras, key=lambda camera: (camera.road.casefold(), camera.description.casefold(), camera.camera_id))


def save_snapshot(camera: Camera, output_dir: Path) -> Path:
    request = urllib.request.Request(camera.image_url, headers={"User-Agent": "TrafficAnalyzer/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            image = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not download snapshot for camera {camera.camera_id}: {exc}") from exc

    if len(image) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"Snapshot for camera {camera.camera_id} exceeds {MAX_IMAGE_BYTES} bytes.")
    if content_type not in {"image/jpeg", "image/png"}:
        raise RuntimeError(
            f"Camera {camera.camera_id} returned {content_type or 'an unknown content type'}, not an image."
        )
    if not (image.startswith(b"\xff\xd8\xff") or image.startswith(b"\x89PNG\r\n\x1a\n")):
        raise RuntimeError(f"Camera {camera.camera_id} response is not a valid JPEG or PNG image.")

    captured_at = datetime.now(timezone.utc)
    extension = ".jpg" if content_type == "image/jpeg" else ".png"
    camera_dir = output_dir / f"camera_{camera.camera_id}"
    camera_dir.mkdir(parents=True, exist_ok=True)
    filename = captured_at.strftime("%Y%m%dT%H%M%S%fZ") + extension
    snapshot_path = camera_dir / filename
    snapshot_path.write_bytes(image)

    manifest = {
        "captured_at": captured_at.isoformat(timespec="milliseconds"),
        "snapshot": snapshot_path.relative_to(output_dir).as_posix(),
        **asdict(camera),
    }
    with (output_dir / "manifest.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=True) + "\n")
    return snapshot_path
