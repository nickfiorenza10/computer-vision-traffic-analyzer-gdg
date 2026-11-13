"""Postgres side of the camera sweep: the catalogue and one row per camera per sweep.

The worker writes with its own login (`traffic_writer`: upsert cameras, read and add sweeps,
never update or delete a sweep), which is what `database/map_schema.sql` grants.
"""

from __future__ import annotations

from datetime import datetime

import psycopg

UPSERT_CAMERAS = """
INSERT INTO traffic.cameras (
    camera_id, district, name, county, route, direction, latitude, longitude,
    has_stream, image_url, stream_url, in_service, updated_at
) VALUES (
    %(camera_id)s, %(district)s, %(name)s, %(county)s, %(route)s, %(direction)s,
    %(latitude)s, %(longitude)s, %(has_stream)s, %(image_url)s, %(stream_url)s,
    true, CURRENT_TIMESTAMP
)
ON CONFLICT (camera_id) DO UPDATE SET
    district = EXCLUDED.district,
    name = EXCLUDED.name,
    county = EXCLUDED.county,
    route = EXCLUDED.route,
    direction = EXCLUDED.direction,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    has_stream = EXCLUDED.has_stream,
    image_url = EXCLUDED.image_url,
    stream_url = EXCLUDED.stream_url,
    in_service = true,
    updated_at = CURRENT_TIMESTAMP
"""

LIST_CAMERAS = """
SELECT camera_id, image_url, latitude, longitude
FROM traffic.cameras
WHERE district = ANY(%(districts)s) AND in_service AND image_url IS NOT NULL
ORDER BY camera_id
"""

HISTORY = """
SELECT vehicles_mean
FROM traffic.camera_sweeps
WHERE camera_id = %(camera_id)s AND vehicles_mean IS NOT NULL
  AND level NOT IN ('night', 'unknown')
ORDER BY swept_at DESC
LIMIT %(limit)s
"""

INSERT_SWEEP = """
INSERT INTO traffic.camera_sweeps (
    camera_id, swept_at, frames, vehicles_mean, vehicles_max, mean_brightness, level, detector
) VALUES (
    %(camera_id)s, %(swept_at)s, %(frames)s, %(vehicles_mean)s, %(vehicles_max)s,
    %(mean_brightness)s, %(level)s, %(detector)s
)
ON CONFLICT (camera_id, swept_at) DO NOTHING
"""


def camera_row(camera) -> dict:
    """One camera as the table wants it: the database names them `caltrans-D3-41`."""
    camera_id = str(camera.camera_id)
    district = int(camera_id.split("-")[0].removeprefix("D"))
    return {
        "camera_id": f"caltrans-{camera_id}",
        "district": district,
        "name": (camera.location or camera.nearby_place or camera_id)[:200],
        "county": camera.county or "",
        "route": camera.route or "",
        "direction": camera.direction or "",
        "latitude": float(camera.latitude),
        "longitude": float(camera.longitude),
        "has_stream": bool(camera.stream_url),
        "image_url": getattr(camera, "image_url", None) or None,
        "stream_url": camera.stream_url or None,
    }


class PostgresStore:
    """One connection per call: the sweep is minutes apart, so pooling would be noise."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, autocommit=True, connect_timeout=15)

    def upsert_cameras(self, cameras) -> int:
        rows = [camera_row(camera) for camera in cameras]
        if not rows:
            return 0
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(UPSERT_CAMERAS, rows)
        return len(rows)

    def list_cameras(self, districts: tuple[int, ...]) -> list[dict]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(LIST_CAMERAS, {"districts": list(districts)})
            return [
                {"camera_id": row[0], "image_url": row[1], "latitude": row[2], "longitude": row[3]}
                for row in cursor.fetchall()
            ]

    def history(self, camera_id: str, limit: int = 60) -> list[float]:
        """Earlier daytime readings only: a night or dark sweep says nothing about how busy
        the road usually is, and zeros from them would make every daytime sweep look heavy."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(HISTORY, {"camera_id": camera_id, "limit": limit})
            return [float(row[0]) for row in cursor.fetchall()]

    def insert_sweep(self, row: dict) -> None:
        payload = dict(row)
        if isinstance(payload.get("swept_at"), datetime):
            payload["swept_at"] = payload["swept_at"]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(INSERT_SWEEP, payload)
