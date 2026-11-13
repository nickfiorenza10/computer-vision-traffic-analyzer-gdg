"""Command-line entry point for FL511 snapshot and Caltrans video ingestion."""

import argparse
import sys
import time
from pathlib import Path

from ingestion import caltrans, fl511_video
from ingestion.fl511 import TAMPA_BBOX, Camera, list_cameras, save_snapshot


def _bbox_arg(value: str) -> tuple[float, float, float, float]:
    try:
        bbox = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use west,south,east,north numeric coordinates.") from exc
    if len(bbox) != 4:
        raise argparse.ArgumentTypeError("Use west,south,east,north numeric coordinates.")
    return bbox


def _add_location_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bbox",
        type=_bbox_arg,
        default=TAMPA_BBOX,
        metavar="W,S,E,N",
        help="Tampa-area longitude/latitude bounds (default: %(default)s)",
    )
    parser.add_argument(
        "--include-highways",
        action="store_true",
        help="Include limited-access highway, ramp, and interchange cameras.",
    )


def _format_camera(camera: Camera) -> str:
    return (
        f"{camera.camera_id:<6} {camera.road[:24]:<24} {camera.description[:44]:<44} "
        f"{camera.direction:<3} {camera.latitude:.6f},{camera.longitude:.6f}"
    )


def _list_command(args: argparse.Namespace) -> None:
    cameras = list_cameras(args.bbox, args.include_highways)
    if not cameras:
        print("No cameras found in the selected Tampa-area bounds.")
        return
    print(f"{'ID':<6} {'ROAD':<24} {'LOCATION':<44} {'DIR':<3} COORDINATES")
    for camera in cameras:
        print(_format_camera(camera))
    print(f"\n{len(cameras)} cameras. Review the camera view before selecting it for AI analysis.")


def _capture_command(args: argparse.Namespace) -> None:
    selected_ids = list(dict.fromkeys(args.camera_id))
    while True:
        cameras = list_cameras(args.bbox, args.include_highways)
        by_id = {camera.camera_id: camera for camera in cameras}
        missing = [camera_id for camera_id in selected_ids if camera_id not in by_id]
        if missing:
            raise ValueError(
                f"Camera ID(s) not found in the selected bounds/filter: {', '.join(missing)}. "
                "Run `python -m ingestion list` to review available cameras."
            )
        for camera_id in selected_ids:
            path = save_snapshot(by_id[camera_id], args.output)
            print(f"Captured camera {camera_id}: {path}")
        if args.once:
            return
        print(f"Next poll in {args.interval_seconds:g} seconds (Ctrl+C to stop).")
        time.sleep(args.interval_seconds)


def _district_arg(value: str) -> int:
    try:
        district = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("District must be a number from 1 to 12.") from exc
    if district not in caltrans.DISTRICTS:
        raise argparse.ArgumentTypeError("District must be a number from 1 to 12.")
    return district


def _caltrans_list_command(args: argparse.Namespace) -> None:
    districts = tuple(dict.fromkeys(args.district)) if args.district else caltrans.DISTRICTS
    cameras = caltrans.list_cameras(districts)
    if args.county:
        cameras = [camera for camera in cameras if camera.county.casefold() == args.county.casefold()]
    if not cameras:
        print("No Caltrans cameras with live video match the selected filters.")
        return
    print(f"{'ID':<8} {'COUNTY':<16} {'ROUTE':<7} {'LOCATION':<40} {'DIR':<10} COORDINATES")
    for camera in cameras:
        print(
            f"{camera.camera_id:<8} {camera.county[:16]:<16} {camera.route[:7]:<7} "
            f"{camera.location[:40]:<40} {camera.direction[:10]:<10} {camera.latitude:.6f},{camera.longitude:.6f}"
        )
    print(f"\n{len(cameras)} cameras with live video. Review the camera view before selecting it for AI analysis.")


def _caltrans_record_command(args: argparse.Namespace) -> None:
    camera = caltrans.find_camera(args.camera_id)
    print(f"Recording {args.minutes:g} min from {camera.camera_id} ({camera.location}). Ctrl+C to stop.")
    path = caltrans.record_stream(camera, args.output, args.minutes * 60)
    print(f"Saved video: {path}")


def _fl511_record_command(args: argparse.Namespace) -> None:
    state = fl511_video.describe_capture_state()
    print(state)
    path = fl511_video.record_fl511_video(
        args.camera_id,
        args.minutes,
        args.output,
        player_url=args.player_url,
    )
    print(f"Saved video: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and ingest FL511 camera snapshots and Caltrans live camera video."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List Tampa-area camera metadata.")
    _add_location_options(listing)
    listing.set_defaults(handler=_list_command)

    capture = commands.add_parser("capture", help="Download snapshots from selected camera IDs.")
    _add_location_options(capture)
    capture.add_argument("--camera-id", action="append", required=True, help="Camera ID; repeat to select multiple.")
    capture.add_argument(
        "--output",
        type=Path,
        default=Path("data/live_footage"),
        help="Snapshot output directory (default: data/live_footage).",
    )
    capture.add_argument(
        "--interval-seconds",
        type=float,
        default=180,
        help="Delay between snapshot polls (default: 180; FL511 refreshes a still about "
        "every three minutes, so polling faster mostly repeats the same frame).",
    )
    capture.add_argument("--once", action="store_true", help="Capture one snapshot from each selected camera.")
    capture.set_defaults(handler=_capture_command)

    caltrans_list = commands.add_parser("caltrans-list", help="List Caltrans cameras that have live video.")
    caltrans_list.add_argument(
        "--district", type=_district_arg, action="append", help="Caltrans district 1-12; repeat for several (default: all)."
    )
    caltrans_list.add_argument("--county", help="Only show cameras in this county, e.g. Sacramento.")
    caltrans_list.set_defaults(handler=_caltrans_list_command)

    caltrans_record = commands.add_parser("caltrans-record", help="Record live video from one Caltrans camera.")
    caltrans_record.add_argument("--camera-id", required=True, help="Camera ID from caltrans-list, e.g. D3-1.")
    caltrans_record.add_argument(
        "--minutes", type=float, default=5, help="Length of video to record (default: 5)."
    )
    caltrans_record.add_argument(
        "--output",
        type=Path,
        default=Path("data/live_footage"),
        help="Video output directory (default: data/live_footage).",
    )
    caltrans_record.set_defaults(handler=_caltrans_record_command)

    fl511_record = commands.add_parser(
        "fl511-record",
        help="Record live video from one FL511 camera through its own page (switched off until FDOT confirms in writing).",
    )
    fl511_record.add_argument("--camera-id", required=True, help="FL511 camera ID from `list`, e.g. 1267.")
    fl511_record.add_argument(
        "--minutes",
        type=float,
        default=5,
        help=f"Length of video to record (default: 5; at most {fl511_video.MAX_MINUTES:g}).",
    )
    fl511_record.add_argument(
        "--output",
        type=Path,
        default=Path("data/live_footage"),
        help="Video output directory (default: data/live_footage).",
    )
    fl511_record.add_argument(
        "--player-url",
        default=None,
        help=(
            "Browse a specific FL511 page instead of using the stream resolver (the page "
            "must already show the camera's card; the camera list is the default)."
        ),
    )
    fl511_record.set_defaults(handler=_fl511_record_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "capture" and args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than zero.")
    if args.command == "caltrans-record" and args.minutes <= 0:
        parser.error("--minutes must be greater than zero.")
    if args.command == "fl511-record" and not 0 < args.minutes <= fl511_video.MAX_MINUTES:
        parser.error(f"--minutes must be between 0 and {fl511_video.MAX_MINUTES:g}.")
    try:
        args.handler(args)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
