"""Compare hand counts (hand_count.py) with the pipeline's events.csv.

Only the frames you reviewed by hand are compared. Each pipeline event is matched to
a hand count in the same lane within a small time tolerance, which gives:
- matched: counted by both
- missed: counted by hand, not by the pipeline
- extra: counted by the pipeline, not by hand (double counts, false detections)

Usage:
    python pipeline/compare_counts.py data/output/vehicles-2_hand.csv data/output/vehicles-2_events.csv
    python pipeline/compare_counts.py hand.csv events.csv --tolerance 0.5 --bin-seconds 10
"""

import argparse
import csv
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Compare hand counts with events.csv.")
    parser.add_argument("hand", type=Path, help="Hand-count CSV from hand_count.py")
    parser.add_argument("events", type=Path, help="events.csv from track_count.py")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="Seconds a pipeline event may differ from the hand count and still match (default: 0.5)",
    )
    parser.add_argument("--bin-seconds", type=float, default=10, help="Bin size for the per-bin error (default: 10)")
    parser.add_argument(
        "--lanes",
        type=int,
        nargs="+",
        default=None,
        help="Only compare these lanes, if you counted some lanes by hand (default: all)",
    )
    return parser.parse_args()


def match_lane(hand_frames, auto_frames, tolerance_frames):
    """Pair hand and pipeline frames in one lane. Both lists must be sorted.

    Walks both lists in time order and pairs the closest frames within the tolerance,
    so each event matches at most one hand count. Returns (matched, missed, extra).
    """
    i = j = matched = 0
    while i < len(hand_frames) and j < len(auto_frames):
        diff = auto_frames[j] - hand_frames[i]
        if abs(diff) <= tolerance_frames:
            matched += 1
            i += 1
            j += 1
        elif diff < 0:
            j += 1  # pipeline event with no hand count near it
        else:
            i += 1  # hand count with no pipeline event near it
    return matched, len(hand_frames) - matched, len(auto_frames) - matched


def compare(hand, auto, start_frame, end_frame, fps, tolerance_seconds, bin_seconds, only_lanes=None):
    """hand and auto are lists of (frame, lane). Returns per-lane rows and an overall row."""
    def in_window(rows):
        return [
            (f, lane) for f, lane in rows
            if start_frame <= f <= end_frame and (only_lanes is None or lane in only_lanes)
        ]

    hand, auto = in_window(hand), in_window(auto)
    lanes = sorted({lane for _, lane in hand} | {lane for _, lane in auto})
    tolerance_frames = tolerance_seconds * fps
    rows = []
    for lane in lanes:
        h = sorted(f for f, ln in hand if ln == lane)
        a = sorted(f for f, ln in auto if ln == lane)
        matched, missed, extra = match_lane(h, a, tolerance_frames)
        rows.append({"lane": lane, "hand": len(h), "pipeline": len(a), "matched": matched, "missed": missed, "extra": extra})

    total = {key: sum(r[key] for r in rows) for key in ("hand", "pipeline", "matched", "missed", "extra")}
    total["lane"] = "all"
    rows.append(total)
    for r in rows:
        r["count_error_pct"] = round(100 * (r["pipeline"] - r["hand"]) / r["hand"], 1) if r["hand"] else None
        r["recall_pct"] = round(100 * r["matched"] / r["hand"], 1) if r["hand"] else None
        r["precision_pct"] = round(100 * r["matched"] / r["pipeline"], 1) if r["pipeline"] else None

    # Mean absolute count error per bin and lane, so offsetting errors don't hide
    bin_frames = bin_seconds * fps
    bins = {}
    for source, items in (("hand", hand), ("pipeline", auto)):
        for f, lane in items:
            key = (int((f - start_frame) // bin_frames), lane)
            bins.setdefault(key, {"hand": 0, "pipeline": 0})[source] += 1
    n_bins = int((end_frame - start_frame) // bin_frames) + 1
    errors = [
        abs(bins.get((b, lane), {}).get("pipeline", 0) - bins.get((b, lane), {}).get("hand", 0))
        for b in range(n_bins)
        for lane in lanes
    ]
    bin_mae = round(sum(errors) / len(errors), 2) if errors else 0.0
    return rows, bin_mae


def read_hand(path):
    with path.open(newline="") as f:
        return [(int(r["frame"]), int(r["lane"])) for r in csv.DictReader(f)]


def read_events(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [(int(r["frame"]), int(r["lane"])) for r in csv.DictReader(f)]


def main():
    args = parse_args()
    meta_path = args.hand.with_suffix(".json")
    if not meta_path.is_file():
        raise SystemExit(f"{meta_path} not found; it is written by hand_count.py next to the CSV")
    meta = json.loads(meta_path.read_text())
    fps, start, end = meta["fps"], meta["start_frame"], meta["end_frame"]

    rows, bin_mae = compare(
        read_hand(args.hand), read_events(args.events), start, end, fps, args.tolerance, args.bin_seconds,
        set(args.lanes) if args.lanes else None,
    )
    print(f"Frames {start}-{end} ({(end - start) / fps:.1f} s of {meta['video']}), tolerance {args.tolerance}s\n")
    header = f"{'lane':>5} {'hand':>5} {'pipeline':>8} {'matched':>7} {'missed':>6} {'extra':>5} {'count err':>9} {'recall':>7} {'precision':>9}"
    print(header)
    print("-" * len(header))

    def pct(v):
        return f"{v:+.1f}%" if v is not None else "-"

    for r in rows:
        print(
            f"{r['lane']:>5} {r['hand']:>5} {r['pipeline']:>8} {r['matched']:>7} {r['missed']:>6} {r['extra']:>5} "
            f"{pct(r['count_error_pct']):>9} {(str(r['recall_pct']) + '%') if r['recall_pct'] is not None else '-':>7} "
            f"{(str(r['precision_pct']) + '%') if r['precision_pct'] is not None else '-':>9}"
        )
    print(f"\nMean absolute error per {args.bin_seconds:g}s bin and lane: {bin_mae} vehicles")


if __name__ == "__main__":
    main()
