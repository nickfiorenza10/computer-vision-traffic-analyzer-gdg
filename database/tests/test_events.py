import ast
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path

from database.load_events import EVENT_COLUMNS, VEHICLE_CLASSES, iter_events, parse_offset

ROOT = Path(__file__).resolve().parents[2]
HEADER = ",".join(EVENT_COLUMNS) + "\n"
VALID = "2026-10-09T08:00:00.033,1,7,car,1,inbound\n"


class CSVContractTests(unittest.TestCase):
    def test_pipeline_contract_matches_without_loading_ml_dependencies(self):
        tree = ast.parse((ROOT / "pipeline/track_count.py").read_text(encoding="utf-8"))
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"EVENT_COLUMNS", "VEHICLE_CLASSES"}
        }
        self.assertEqual(values["EVENT_COLUMNS"], EVENT_COLUMNS)
        self.assertEqual(set(values["VEHICLE_CLASSES"].values()), VEHICLE_CLASSES)

    def test_sample_csv(self):
        with (ROOT / "data/sample_events.csv").open(encoding="utf-8", newline="") as stream:
            events = list(iter_events(stream, "+00:00"))
        self.assertEqual(len(events), 787)
        self.assertEqual({e[4] for e in events}, {1, 2, 3, 4, 5, 6})

    def test_offset_required_for_naive_time(self):
        with self.assertRaisesRegex(ValueError, "line 2.*no timezone"):
            list(iter_events(io.StringIO(HEADER + VALID)))

    def test_offset_normalizes_to_utc(self):
        row = next(iter_events(io.StringIO(HEADER + VALID), "-04:00"))
        self.assertEqual(row[0], datetime(2026, 10, 9, 12, 0, 0, 33000, timezone.utc))

    def test_explicit_offsets_override_naive_fallback(self):
        for stamp in ["2026-10-09T08:00:00Z", "2026-10-09T10:00:00+02:00"]:
            with self.subTest(stamp=stamp):
                row = stamp + ",1,7,car,1,in\n"
                event = next(iter_events(io.StringIO(HEADER + row), "-04:00"))
                self.assertEqual(event[0], datetime(2026, 10, 9, 8, tzinfo=timezone.utc))
                self.assertEqual(event[-1], "in")  # Pipeline directions are configurable.

    def test_header_only_is_valid(self):
        self.assertEqual(list(iter_events(io.StringIO(HEADER))), [])

    def test_schema_drift_rejected(self):
        for header in ["", HEADER.replace("class", "vehicle_class"), HEADER.rstrip() + ",extra\n"]:
            with self.subTest(header=header), self.assertRaisesRegex(ValueError, "header"):
                list(iter_events(io.StringIO(header)))

    def test_invalid_rows_report_line(self):
        changes = {
            "timestamp": ["2026-10-09", "2026-02-30T00:00:00Z", "2026-10-09T08:00:00+00:60"],
            "frame": ["-1", "1.5", str(2**63)],
            "track_id": ["-1", "", str(2**63)],
            "class": ["person", "CAR"],
            "lane": ["0", "-1", str(2**31)],
            "direction": ["", " ", "inbound "],
        }
        for column, bad_values in changes.items():
            for value in bad_values:
                fields = VALID.rstrip("\n").split(",")
                fields[EVENT_COLUMNS.index(column)] = value
                with self.subTest(column=column, value=value), self.assertRaisesRegex(ValueError, "line 2"):
                    list(iter_events(io.StringIO(HEADER + ",".join(fields) + "\n"), "+00:00"))

    def test_extra_and_missing_fields_rejected(self):
        for row in [VALID.rstrip() + ",extra\n", "2026-10-09T08:00:00Z,1,7,car,1\n"]:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "line 2"):
                list(iter_events(io.StringIO(HEADER + row), "+00:00"))

    def test_duplicate_track_rejected(self):
        with self.assertRaisesRegex(ValueError, "line 3.*duplicate track_id"):
            list(iter_events(io.StringIO(HEADER + VALID + VALID), "+00:00"))

    def test_invalid_offset(self):
        for value in ["UTC", "04:00", "+24:00", "+01:60"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_offset(value)


if __name__ == "__main__":
    unittest.main()
