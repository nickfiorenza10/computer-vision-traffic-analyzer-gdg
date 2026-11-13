"""Sunrise, sunset and civil twilight for a camera's own coordinates, computed offline.

Brightness alone confuses a dark camera at noon with a camera at night, and flickers at dusk.
The sun's position does not: given a camera's latitude and longitude, `phase` says whether it
is day, twilight or night there, and the sweep, live analysis and the map all use the same
answer for California and Florida cameras alike.

The math is the standard sunrise equation (as on Wikipedia's "Sunrise equation" page, after
the NOAA and Meeus approximations). It is good to about a minute at these latitudes, checked
against the US Naval Observatory in the tests. No network, no dependencies.

All times are timezone-aware UTC datetimes; convert for display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

DAY = "day"
TWILIGHT = "twilight"
NIGHT = "night"

# The sun's centre this far below the horizon counts as rise or set: half its disc plus
# atmospheric refraction.
SUNRISE_ALTITUDE = -0.833
# Civil twilight ends at 6 degrees below; past that, roadside cameras are effectively dark.
CIVIL_ALTITUDE = -6.0

_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5
_OBLIQUITY = math.radians(23.4397)


@dataclass(frozen=True)
class SunTimes:
    """One local solar day. A field is None when the sun never crosses that altitude
    (polar day or night); that never happens in California or Florida."""

    dawn: datetime | None
    sunrise: datetime | None
    noon: datetime
    sunset: datetime | None
    dusk: datetime | None
    noon_altitude: float  # degrees; decides polar day versus polar night


def _to_julian(when: datetime) -> float:
    return when.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def _from_julian(jd: float) -> datetime:
    return datetime.fromtimestamp((jd - _UNIX_EPOCH_JD) * 86400.0, tz=timezone.utc)


def sun_times(lat: float, lon: float, day: date) -> SunTimes:
    """Civil dawn, sunrise, solar noon, sunset and civil dusk for `day` at a place.

    `day` is the local calendar date there (longitude decides which solar noon is meant),
    `lon` is degrees east, so California and Florida are negative.
    """
    # Julian day number of local noon for that date, approximately.
    noon_utc = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    n = round(_to_julian(noon_utc) - _J2000 - lon / 360.0)
    mean_noon = n - lon / 360.0

    anomaly = math.radians((357.5291 + 0.98560028 * mean_noon) % 360.0)
    centre = (
        1.9148 * math.sin(anomaly)
        + 0.0200 * math.sin(2 * anomaly)
        + 0.0003 * math.sin(3 * anomaly)
    )
    ecliptic_lon = math.radians((math.degrees(anomaly) + centre + 180.0 + 102.9372) % 360.0)
    transit = _J2000 + mean_noon + 0.0053 * math.sin(anomaly) - 0.0069 * math.sin(2 * ecliptic_lon)
    declination = math.asin(math.sin(ecliptic_lon) * math.sin(_OBLIQUITY))

    def crossing(altitude: float) -> tuple[datetime | None, datetime | None]:
        phi = math.radians(lat)
        cos_hour = (math.sin(math.radians(altitude)) - math.sin(phi) * math.sin(declination)) / (
            math.cos(phi) * math.cos(declination)
        )
        if not -1.0 <= cos_hour <= 1.0:
            return None, None
        half = math.degrees(math.acos(cos_hour)) / 360.0
        return _from_julian(transit - half), _from_julian(transit + half)

    dawn, dusk = crossing(CIVIL_ALTITUDE)
    sunrise, sunset = crossing(SUNRISE_ALTITUDE)
    noon_altitude = 90.0 - abs(lat - math.degrees(declination))
    return SunTimes(dawn, sunrise, _from_julian(transit), sunset, dusk, noon_altitude)


def phase(lat: float, lon: float, when: datetime) -> str:
    """'day' between sunrise and sunset, 'twilight' in civil dawn or dusk, else 'night'."""
    if when.tzinfo is None:
        raise ValueError("phase needs a timezone-aware datetime")
    when = when.astimezone(timezone.utc)
    # The solar day containing `when` is the one whose noon is nearest.
    local_day = (when + timedelta(hours=lon / 15.0)).date()
    times = sun_times(lat, lon, local_day)
    if times.sunrise and times.sunset and times.sunrise <= when < times.sunset:
        return DAY
    if times.dawn and times.dusk and times.dawn <= when < times.dusk:
        return TWILIGHT
    if times.sunrise is None:
        # Polar day or night: the sun stays on one side of the horizon all day.
        if times.noon_altitude > SUNRISE_ALTITUDE:
            return DAY
        return TWILIGHT if times.noon_altitude > CIVIL_ALTITUDE else NIGHT
    if times.dawn is None:
        return TWILIGHT  # high-latitude summer night: the sun never gets 6 degrees down
    return NIGHT


def next_sunrise(lat: float, lon: float, after: datetime) -> datetime:
    """The first sunrise strictly after `after`, for scheduling daytime-only work."""
    after = after.astimezone(timezone.utc)
    start = (after + timedelta(hours=lon / 15.0)).date()
    for offset in range(0, 366):
        rise = sun_times(lat, lon, start + timedelta(days=offset)).sunrise
        if rise and rise > after:
            return rise
    raise ValueError("no sunrise within a year at this latitude")


def daylight_wait(lat: float, lon: float, when: datetime) -> str | None:
    """None if it is day at the place, else a sentence saying when daylight work can start.

    For jobs that only make sense in daylight (learning lanes, precision snapshots): refuse
    with this message rather than run on a dark frame. The time is given in UTC; callers
    with a local zone can convert it.
    """
    if phase(lat, lon, when) == DAY:
        return None
    rise = next_sunrise(lat, lon, when)
    return f"It is not daylight at ({lat:.4f}, {lon:.4f}); the next sunrise is {rise:%Y-%m-%d %H:%M} UTC."


def main(argv: list[str] | None = None) -> int:
    """Print today's sun times and the current phase for a place, in its local zone."""
    import argparse
    from zoneinfo import ZoneInfo

    parser = argparse.ArgumentParser(description="Sun times and day/twilight/night for a camera.")
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--zone", default=None, help="IANA zone for display (default: guessed for CA/FL)")
    args = parser.parse_args(argv)
    zone = ZoneInfo(args.zone or ("America/New_York" if args.longitude > -90 else "America/Los_Angeles"))
    now = datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    times = sun_times(args.latitude, args.longitude, today)

    def local(t: datetime | None) -> str:
        return t.astimezone(zone).strftime("%H:%M") if t else "--"

    print(f"{today} ({zone.key})")
    print(f"dawn {local(times.dawn)}  sunrise {local(times.sunrise)}  "
          f"sunset {local(times.sunset)}  dusk {local(times.dusk)}")
    print(f"now: {phase(args.latitude, args.longitude, now)}")
    wait = daylight_wait(args.latitude, args.longitude, now)
    if wait:
        print(f"next sunrise: {local(next_sunrise(args.latitude, args.longitude, now))} local")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
