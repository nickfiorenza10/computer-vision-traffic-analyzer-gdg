"""Set TEST_DATABASE_URL to an explicitly disposable PostgreSQL database."""

import csv
import hashlib
import io
import os
import unittest
import uuid
from collections import Counter
from pathlib import Path

from database.load_events import import_events
from database.tests.test_events import HEADER, VALID

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL tests")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        cls.connection = psycopg.connect(
            os.environ["TEST_DATABASE_URL"], autocommit=True, connect_timeout=10
        )
        # Reapplying this initial schema should be safe during local setup.
        schema = (ROOT / "database/schema.sql").read_text(encoding="utf-8")
        cls.connection.execute(schema)
        cls.connection.execute(schema)
        cls.connection.execute("SET TIME ZONE 'UTC'")

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def setUp(self):
        self.camera = "test-camera-" + uuid.uuid4().hex
        self.sources = []

    def tearDown(self):
        # Remove only rows created by this test, never drop a database/schema.
        for source in self.sources:
            self.connection.execute("DELETE FROM traffic.events WHERE source_id = %s", (source,))
            self.connection.execute("DELETE FROM traffic.sources WHERE source_id = %s", (source,))

    def load(self, content, *, source=None, camera=None, count=None, offset="+00:00"):
        source = source or "test-source-" + uuid.uuid4().hex
        self.sources.append(source)
        inserted = import_events(
            self.connection, io.StringIO(content), source_id=source,
            camera_id=camera or self.camera, filename="test.csv",
            digest=hashlib.sha256(content.encode()).hexdigest(), naive_offset=offset,
            event_count=count if count is not None else len(list(csv.DictReader(io.StringIO(content)))),
        )
        return source, inserted

    def totals(self, start="2026-10-09T08:00:00Z", end="2026-10-09T08:01:00Z"):
        return self.connection.execute(
            "SELECT * FROM traffic.lane_totals(%s, %s, %s)", (self.camera, start, end)
        ).fetchall()

    def test_sample_aggregations_and_retry(self):
        content = (ROOT / "data/sample_events.csv").read_text(encoding="utf-8")
        source, inserted = self.load(content)
        self.assertTrue(inserted)
        self.assertFalse(self.load(content, source=source)[1])
        raw = list(csv.DictReader(io.StringIO(content)))
        expected = Counter((int(e["lane"]), e["direction"]) for e in raw)
        self.assertEqual(self.totals(), [(lane, direction, count) for (lane, direction), count in sorted(expected.items())])
        self.assertEqual(sum(row[-1] for row in self.totals()), 787)
        bins = self.connection.execute(
            "SELECT * FROM traffic.time_bins(%s, %s, %s, 10)",
            (self.camera, "2026-10-09T08:00:00Z", "2026-10-09T08:01:00Z"),
        ).fetchall()
        expected_bins = Counter(
            (int(e["timestamp"][17:19]) // 10 * 10, int(e["lane"]), e["direction"]) for e in raw
        )
        self.assertEqual([(row[0].second, *row[1:]) for row in bins],
                         [(*key, count) for key, count in sorted(expected_bins.items())])

    def test_sources_share_track_ids_and_cameras_are_isolated(self):
        self.load(HEADER + VALID)
        self.load(HEADER + VALID)
        self.load(HEADER + VALID, camera="other-" + self.camera)
        self.assertEqual(self.totals(), [(1, "inbound", 2)])

    def test_changed_file_or_metadata_rejected(self):
        source, _ = self.load(HEADER + VALID)
        for changes in [dict(content=HEADER + VALID.replace("car", "truck")),
                        dict(content=HEADER + VALID, camera="changed"),
                        dict(content=HEADER + VALID, offset="-04:00")]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "different content or metadata"):
                self.load(source=source, **changes)
        self.assertEqual(self.totals(), [(1, "inbound", 1)])

    def test_failed_copy_rolls_back_source_and_events(self):
        source = "test-source-" + uuid.uuid4().hex
        bad = VALID.replace(",7,car,", ",8,person,")
        with self.assertRaisesRegex(ValueError, "unknown vehicle class"):
            self.load(HEADER + VALID + bad, source=source)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM traffic.sources WHERE source_id = %s", (source,)
        ).fetchone()[0], 0)
        self.assertEqual(self.totals(), [])

    def test_half_open_window_and_bin_edges(self):
        rows = [f"2026-10-09T08:00:{second},1,{index},car,1,in\n"
                for index, second in enumerate(["00Z", "09.999Z", "10Z", "20Z"])]
        self.load(HEADER + "".join(rows))
        self.assertEqual(self.totals(end="2026-10-09T08:00:20Z"), [(1, "in", 3)])
        bins = self.connection.execute(
            "SELECT * FROM traffic.time_bins(%s, %s, %s, 10)",
            (self.camera, "2026-10-09T08:00:00Z", "2026-10-09T08:00:20Z"),
        ).fetchall()
        self.assertEqual([(b[0].second, b[-1]) for b in bins], [(0, 2), (10, 1)])

    def test_invalid_query_parameters(self):
        for start, end in [(None, "2026-01-01Z"), ("infinity", "infinity"),
                           ("2026-10-10T00:00:00Z", "2026-10-09T00:00:00Z")]:
            with self.subTest(start=start), self.assertRaises(self.psycopg.Error):
                self.totals(start, end)
        for stride in [None, 0, -1, 86401]:
            with self.subTest(stride=stride), self.assertRaises(self.psycopg.errors.InvalidParameterValue):
                self.connection.execute(
                    "SELECT * FROM traffic.time_bins(%s, %s, %s, %s)",
                    (self.camera, "2026-10-09T08:00:00Z", "2026-10-09T08:01:00Z", stride),
                )

    def test_empty_file_and_empty_camera(self):
        self.load(HEADER)
        self.assertEqual(self.totals(), [])

    def test_timezone_conversion_and_database_constraints(self):
        source, _ = self.load(HEADER + VALID, offset="-04:00")
        self.assertEqual(self.totals(), [])
        self.assertEqual(self.totals("2026-10-09T12:00:00Z", "2026-10-09T12:01:00Z"), [(1, "inbound", 1)])
        for assignment in ["lane = 0", "frame = -1", "vehicle_class = 'person'", "direction = ''"]:
            with self.subTest(assignment=assignment), self.assertRaises(self.psycopg.errors.CheckViolation):
                self.connection.execute(
                    f"UPDATE traffic.events SET {assignment} WHERE source_id = %s", (source,)
                )


if __name__ == "__main__":
    unittest.main()
