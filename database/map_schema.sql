-- Live traffic map (Phase 10): the camera catalog and the 30-minute sweep results.
-- Run after schema.sql, as the database owner. Safe to re-run.
--
--   traffic.cameras        one row per Caltrans camera (all districts), refreshed daily
--   traffic.camera_sweeps  one row per camera per sweep: 8 stills analysed for density
--   traffic.camera_map()   every camera with its latest sweep, for GET /api/map/cameras
--
-- Grants, applied only if the roles exist (database/api_readonly_role.sql,
-- database/worker_writer_role.sql):
--   traffic_api     SELECT on both tables and EXECUTE on camera_map()
--   traffic_writer  upsert cameras, read and add sweeps (it needs its own history to rank
--                   a new sweep against the camera's usual traffic)

BEGIN;

CREATE TABLE IF NOT EXISTS traffic.cameras (
    camera_id   text PRIMARY KEY CHECK (camera_id ~ '^caltrans-D([1-9]|1[0-2])-[0-9]{1,5}$'),
    district    smallint NOT NULL CHECK (district BETWEEN 1 AND 12),
    name        text NOT NULL CHECK (btrim(name) <> ''),
    county      text NOT NULL DEFAULT '',
    route       text NOT NULL DEFAULT '',
    direction   text NOT NULL DEFAULT '',
    latitude    double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude   double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    has_stream  boolean NOT NULL,
    image_url   text CHECK (image_url IS NULL OR image_url LIKE 'https://%'),
    stream_url  text CHECK (stream_url IS NULL OR stream_url LIKE 'https://%'),
    in_service  boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS traffic.camera_sweeps (
    camera_id       text NOT NULL REFERENCES traffic.cameras (camera_id) ON DELETE CASCADE,
    swept_at        timestamptz NOT NULL CHECK (isfinite(swept_at)),
    frames          smallint NOT NULL CHECK (frames BETWEEN 0 AND 20),
    vehicles_mean   real CHECK (vehicles_mean IS NULL OR vehicles_mean >= 0),
    vehicles_max    smallint CHECK (vehicles_max IS NULL OR vehicles_max >= 0),
    mean_brightness real CHECK (mean_brightness IS NULL OR mean_brightness BETWEEN 0 AND 255),
    level           text NOT NULL CHECK (level IN ('heavy', 'moderate', 'light', 'learning', 'night', 'unknown')),
    detector        text NOT NULL DEFAULT 'yolo' CHECK (btrim(detector) <> ''),
    PRIMARY KEY (camera_id, swept_at)
);

-- Latest sweep per camera, and a camera's history for its percentile baseline.
CREATE INDEX IF NOT EXISTS camera_sweeps_latest_idx ON traffic.camera_sweeps (camera_id, swept_at DESC);
CREATE INDEX IF NOT EXISTS cameras_district_idx ON traffic.cameras (district);

CREATE OR REPLACE FUNCTION traffic.camera_map()
RETURNS TABLE (
    camera_id text, name text, county text, route text, direction text,
    latitude double precision, longitude double precision, has_stream boolean,
    image_url text, swept boolean, level text, vehicles_mean real, swept_at timestamptz
)
LANGUAGE sql STABLE AS $$
    SELECT c.camera_id, c.name, c.county, c.route, c.direction,
           c.latitude, c.longitude, c.has_stream, c.image_url,
           s.camera_id IS NOT NULL, s.level, s.vehicles_mean, s.swept_at
    FROM traffic.cameras c
    LEFT JOIN LATERAL (
        SELECT cs.camera_id, cs.level, cs.vehicles_mean, cs.swept_at
        FROM traffic.camera_sweeps cs
        WHERE cs.camera_id = c.camera_id
        ORDER BY cs.swept_at DESC
        LIMIT 1
    ) s ON true
    WHERE c.in_service
    ORDER BY c.camera_id;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'traffic_api') THEN
        GRANT SELECT ON traffic.cameras, traffic.camera_sweeps TO traffic_api;
        GRANT EXECUTE ON FUNCTION traffic.camera_map() TO traffic_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'traffic_writer') THEN
        GRANT SELECT, INSERT, UPDATE ON traffic.cameras TO traffic_writer;
        GRANT SELECT, INSERT ON traffic.camera_sweeps TO traffic_writer;
    END IF;
END
$$;

COMMIT;
