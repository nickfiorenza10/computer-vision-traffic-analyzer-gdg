-- Initial schema, PostgreSQL 16+. Run with psql -v ON_ERROR_STOP=1.
BEGIN;

CREATE SCHEMA IF NOT EXISTS traffic;

CREATE TABLE IF NOT EXISTS traffic.sources (
    source_id text PRIMARY KEY CHECK (btrim(source_id) <> ''),
    camera_id text NOT NULL CHECK (btrim(camera_id) <> ''),
    csv_filename text NOT NULL,
    csv_sha256 text NOT NULL CHECK (csv_sha256 ~ '^[0-9a-f]{64}$'),
    naive_offset text,
    event_count bigint NOT NULL CHECK (event_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS traffic.events (
    source_id text NOT NULL REFERENCES traffic.sources (source_id),
    occurred_at timestamptz NOT NULL CHECK (isfinite(occurred_at)),
    frame bigint NOT NULL CHECK (frame >= 0),
    track_id bigint NOT NULL CHECK (track_id >= 0),
    vehicle_class text NOT NULL CHECK (
        vehicle_class IN ('car', 'truck', 'bus', 'motorcycle', 'bicycle')
    ),
    lane integer NOT NULL CHECK (lane > 0),
    direction text NOT NULL CHECK (btrim(direction) <> ''),
    PRIMARY KEY (source_id, track_id)
);

-- Equality on source/camera precedes the event-time range.
CREATE INDEX IF NOT EXISTS sources_camera_idx ON traffic.sources (camera_id);
CREATE INDEX IF NOT EXISTS events_source_time_idx
    ON traffic.events (source_id, occurred_at);

CREATE OR REPLACE FUNCTION traffic.lane_totals(
    p_camera_id text, p_start timestamptz, p_end timestamptz
)
RETURNS TABLE (lane integer, direction text, vehicle_count bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_camera_id IS NULL OR btrim(p_camera_id) = ''
       OR p_start IS NULL OR p_end IS NULL
       OR NOT isfinite(p_start) OR NOT isfinite(p_end) OR p_start >= p_end THEN
        RAISE EXCEPTION 'camera_id and a finite start < end are required'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        SELECT e.lane, e.direction, count(*)
        FROM traffic.events e
        JOIN traffic.sources s ON s.source_id = e.source_id
        WHERE s.camera_id = p_camera_id
          AND e.occurred_at >= p_start AND e.occurred_at < p_end
        GROUP BY e.lane, e.direction
        ORDER BY e.lane, e.direction;
END;
$$;

CREATE OR REPLACE FUNCTION traffic.time_bins(
    p_camera_id text, p_start timestamptz, p_end timestamptz,
    p_bin_seconds integer
)
RETURNS TABLE (
    bin_start timestamptz, lane integer, direction text, vehicle_count bigint
)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_camera_id IS NULL OR btrim(p_camera_id) = ''
       OR p_start IS NULL OR p_end IS NULL
       OR NOT isfinite(p_start) OR NOT isfinite(p_end) OR p_start >= p_end THEN
        RAISE EXCEPTION 'camera_id and a finite start < end are required'
            USING ERRCODE = '22023';
    END IF;
    IF p_bin_seconds IS NULL OR p_bin_seconds < 1 OR p_bin_seconds > 86400 THEN
        RAISE EXCEPTION 'bin_seconds must be between 1 and 86400'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        SELECT date_bin(make_interval(secs => p_bin_seconds), e.occurred_at,
                        TIMESTAMPTZ '1970-01-01 00:00:00+00'),
               e.lane, e.direction, count(*)
        FROM traffic.events e
        JOIN traffic.sources s ON s.source_id = e.source_id
        WHERE s.camera_id = p_camera_id
          AND e.occurred_at >= p_start AND e.occurred_at < p_end
        GROUP BY 1, e.lane, e.direction
        ORDER BY 1, e.lane, e.direction;
END;
$$;

COMMIT;
