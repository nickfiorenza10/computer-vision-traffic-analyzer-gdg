-- Runnable sample queries. The fixture's wall clock is assumed to be UTC here.
-- Match these bounds to the offset used when importing your own footage.
SET TIME ZONE 'UTC';

SELECT * FROM traffic.lane_totals(
    'sample-camera', '2026-10-09T08:00:00Z', '2026-10-09T08:01:00Z'
);

SELECT * FROM traffic.time_bins(
    'sample-camera', '2026-10-09T08:00:00Z', '2026-10-09T08:01:00Z', 10
);

-- Class breakdown over the same half-open time window.
SELECT e.lane, e.direction, e.vehicle_class, count(*) AS vehicle_count
FROM traffic.events e
JOIN traffic.sources s ON s.source_id = e.source_id
WHERE s.camera_id = 'sample-camera'
  AND e.occurred_at >= TIMESTAMPTZ '2026-10-09T08:00:00Z'
  AND e.occurred_at < TIMESTAMPTZ '2026-10-09T08:01:00Z'
GROUP BY e.lane, e.direction, e.vehicle_class
ORDER BY e.lane, e.direction, e.vehicle_class;
