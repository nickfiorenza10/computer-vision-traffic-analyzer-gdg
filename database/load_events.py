"""Validate pipeline CSVs and atomically load them into PostgreSQL.

Run from the repository root: python -m database.load_events --help
Only database imports require psycopg; --validate-only uses the standard library.
"""

import argparse
import csv
import hashlib
import io
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVENT_COLUMNS = ["timestamp", "frame", "track_id", "class", "lane", "direction"]
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle", "bicycle"}
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?"
)


def parse_offset(value):
    """An explicit fixed offset avoids guessing timezone or DST rules."""
    if not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise ValueError("naive offset must use +HH:MM or -HH:MM")
    hours, minutes = map(int, value[1:].split(":"))
    if hours > 23 or minutes > 59:
        raise ValueError("invalid UTC offset")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if value[0] == "+" else -delta)


def parse_timestamp(value, naive_offset):
    if not TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError("timestamp must be ISO 8601 with date, T, and seconds")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Validate offsets ourselves: datetime normalizes offsets such as +00:60.
    if value[-6:-5] in {"+", "-"}:
        parse_offset(value[-6:])
    if parsed.tzinfo is None:
        if naive_offset is None:
            raise ValueError("timestamp has no timezone; supply --naive-offset")
        parsed = parsed.replace(tzinfo=parse_offset(naive_offset))
    return parsed.astimezone(timezone.utc)


def parse_integer(value, name, minimum, maximum):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def iter_events(stream, naive_offset=None):
    """Yield typed rows in database order, rejecting drift or bad records."""
    reader = csv.DictReader(stream, strict=True)
    if reader.fieldnames != EVENT_COLUMNS:
        raise ValueError(f"CSV header must be exactly {','.join(EVENT_COLUMNS)}")
    seen_tracks = set()
    try:
        for row in reader:
            try:
                if None in row or any(value is None or not value.strip() for value in row.values()):
                    raise ValueError("all six columns are required; extra columns are not allowed")
                occurred_at = parse_timestamp(row["timestamp"], naive_offset)
                frame = parse_integer(row["frame"], "frame", 0, 2**63 - 1)
                track_id = parse_integer(row["track_id"], "track_id", 0, 2**63 - 1)
                lane = parse_integer(row["lane"], "lane", 1, 2**31 - 1)
                if row["class"] not in VEHICLE_CLASSES:
                    raise ValueError(f"unknown vehicle class: {row['class']!r}")
                if row["direction"] != row["direction"].strip():
                    raise ValueError("direction must not have surrounding whitespace")
                if track_id in seen_tracks:
                    raise ValueError(f"duplicate track_id {track_id} within one source")
                seen_tracks.add(track_id)
                yield (occurred_at, frame, track_id, row["class"], lane, row["direction"])
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    except csv.Error as exc:
        raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc


def import_events(connection, stream, *, source_id, camera_id, filename, digest,
                  naive_offset, event_count):
    """One transaction per file; INSERT arbitration also handles concurrent retries."""
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO traffic.sources
                   (source_id, camera_id, csv_filename, csv_sha256, naive_offset, event_count)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source_id) DO NOTHING RETURNING source_id""",
                (source_id, camera_id, filename, digest, naive_offset, event_count),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """SELECT camera_id, csv_sha256, naive_offset, event_count
                       FROM traffic.sources WHERE source_id = %s""", (source_id,),
                )
                if cursor.fetchone() != (camera_id, digest, naive_offset, event_count):
                    raise ValueError(
                        "source ID already exists with different content or metadata; "
                        "do not re-import changed output as a new source for the same footage"
                    )
                return False
            with cursor.copy(
                """COPY traffic.events
                   (source_id, occurred_at, frame, track_id, vehicle_class, lane, direction)
                   FROM STDIN"""
            ) as copy:
                actual_count = 0
                for event in iter_events(stream, naive_offset):
                    copy.write_row((source_id, *event))
                    actual_count += 1
                if actual_count != event_count:
                    raise ValueError("validated event count changed before import")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--source-id", help="Stable unique ID for this recording/run")
    parser.add_argument("--camera-id", help="Stable ID for the camera and lane layout")
    parser.add_argument("--naive-offset", help="UTC offset for timestamps without one, e.g. +00:00")
    parser.add_argument("--validate-only", action="store_true", help="Check CSV without a database")
    args = parser.parse_args(argv)
    if not args.validate_only and (not args.source_id or not args.camera_id):
        parser.error("imports require --source-id and --camera-id")
    if any(value is not None and not value.strip() for value in (args.source_id, args.camera_id)):
        parser.error("source and camera IDs must not be blank")
    try:
        if args.naive_offset is not None:
            parse_offset(args.naive_offset)
        # Freeze bytes before validation, hashing and COPY so a changing pipeline
        # output cannot invalidate the checksum or leave a partial import.
        with tempfile.TemporaryFile() as snapshot:
            digest = hashlib.sha256()
            with args.csv_path.open("rb") as original:
                while block := original.read(1024 * 1024):
                    digest.update(block)
                    snapshot.write(block)
            snapshot.seek(0)
            with io.TextIOWrapper(snapshot, encoding="utf-8-sig", newline="") as stream:
                count = sum(1 for _ in iter_events(stream, args.naive_offset))
                if args.validate_only:
                    print(f"Valid CSV: {count} events")
                    return 0
                database_url = os.environ.get("DATABASE_URL")
                if not database_url:
                    raise ValueError("set DATABASE_URL before importing")
                try:
                    import psycopg
                except ImportError as exc:
                    raise ValueError("install database/requirements.txt before importing") from exc
                stream.seek(0)
                with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as connection:
                    inserted = import_events(
                        connection, stream, source_id=args.source_id, camera_id=args.camera_id,
                        filename=args.csv_path.name, digest=digest.hexdigest(),
                        naive_offset=args.naive_offset, event_count=count,
                    )
                print(f"{'Imported' if inserted else 'Already imported'}: {count} events ({args.source_id})")
                return 0
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    except Exception as exc:
        # Do not echo the connection URL (which may contain credentials).
        parser.exit(1, f"Database import failed ({type(exc).__name__}): {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
