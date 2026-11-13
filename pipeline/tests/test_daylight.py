"""Tests for sun times, checked against the US Naval Observatory (aa.usno.navy.mil/api/rstt)."""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import daylight as dl  # noqa: E402

SACRAMENTO = (38.5816, -121.4944)
TAMPA = (27.9506, -82.4572)
PDT = timezone(timedelta(hours=-7))
PST = timezone(timedelta(hours=-8))
EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))


def local(tz, day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


class UsnoReferenceTests(unittest.TestCase):
    """USNO rounds to the minute, so two minutes of slack covers rounding plus the model."""

    CASES = [
        # place, date, zone, dawn, sunrise, sunset, dusk
        (SACRAMENTO, date(2026, 10, 29), PDT, "07:02", "07:30", "18:09", "18:37"),
        (TAMPA, date(2026, 10, 29), EDT, "07:15", "07:39", "18:48", "19:12"),
        (SACRAMENTO, date(2026, 6, 21), PDT, "05:10", "05:42", "20:34", "21:06"),
        (TAMPA, date(2026, 12, 21), EST, "06:51", "07:17", "17:39", "18:05"),
    ]

    def test_matches_the_naval_observatory(self):
        for (lat, lon), day, tz, dawn, rise, set_, dusk in self.CASES:
            times = dl.sun_times(lat, lon, day)
            for got, want in (
                (times.dawn, dawn),
                (times.sunrise, rise),
                (times.sunset, set_),
                (times.dusk, dusk),
            ):
                with self.subTest(place=(lat, lon), day=day, want=want):
                    delta = abs((got - local(tz, day, want)).total_seconds())
                    self.assertLess(delta, 120)

    def test_times_are_utc_and_ordered(self):
        times = dl.sun_times(*SACRAMENTO, date(2026, 10, 29))
        self.assertEqual(times.sunrise.utcoffset(), timedelta(0))
        self.assertTrue(times.dawn < times.sunrise < times.noon < times.sunset < times.dusk)


class PhaseTests(unittest.TestCase):
    def test_day_twilight_and_night_in_sacramento(self):
        day = date(2026, 10, 29)
        self.assertEqual(dl.phase(*SACRAMENTO, local(PDT, day, "12:00")), dl.DAY)
        self.assertEqual(dl.phase(*SACRAMENTO, local(PDT, day, "18:20")), dl.TWILIGHT)
        self.assertEqual(dl.phase(*SACRAMENTO, local(PDT, day, "07:10")), dl.TWILIGHT)
        self.assertEqual(dl.phase(*SACRAMENTO, local(PDT, day, "02:00")), dl.NIGHT)
        self.assertEqual(dl.phase(*SACRAMENTO, local(PDT, day, "23:30")), dl.NIGHT)

    def test_same_clock_time_differs_by_place(self):
        # 7:45 EDT is past sunrise in Tampa but only 4:45 am in Sacramento.
        when = local(EDT, date(2026, 10, 29), "07:45")
        self.assertEqual(dl.phase(*TAMPA, when), dl.DAY)
        self.assertEqual(dl.phase(*SACRAMENTO, when), dl.NIGHT)

    def test_accepts_any_timezone(self):
        noon_utc = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)  # 1 pm PDT
        self.assertEqual(dl.phase(*SACRAMENTO, noon_utc), dl.DAY)

    def test_rejects_naive_datetimes(self):
        with self.assertRaises(ValueError):
            dl.phase(*SACRAMENTO, datetime(2026, 10, 29, 12, 0))

    def test_polar_day_and_night(self):
        svalbard = (78.2, 15.6)
        self.assertEqual(dl.phase(*svalbard, datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)), dl.DAY)
        self.assertEqual(dl.phase(*svalbard, datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc)), dl.NIGHT)
        self.assertIsNone(dl.sun_times(*svalbard, date(2026, 6, 21)).sunset)


class NextSunriseTests(unittest.TestCase):
    def test_tonight_points_at_tomorrow_morning(self):
        rise = dl.next_sunrise(*SACRAMENTO, local(PDT, date(2026, 10, 29), "21:00"))
        self.assertEqual(rise.astimezone(PDT).date(), date(2026, 10, 30))
        self.assertEqual(rise.astimezone(PDT).hour, 7)

    def test_before_dawn_points_at_this_morning(self):
        rise = dl.next_sunrise(*TAMPA, local(EDT, date(2026, 10, 29), "05:00"))
        self.assertLess(abs((rise - local(EDT, date(2026, 10, 29), "07:39")).total_seconds()), 120)


class DaylightWaitTests(unittest.TestCase):
    def test_nothing_to_wait_for_in_daylight(self):
        self.assertIsNone(dl.daylight_wait(*TAMPA, local(EDT, date(2026, 10, 29), "12:00")))

    def test_at_night_it_names_the_next_sunrise(self):
        message = dl.daylight_wait(*TAMPA, local(EDT, date(2026, 10, 29), "22:00"))
        self.assertIn("2026-10-30 11:39 UTC", message)  # 07:39 EDT


if __name__ == "__main__":
    unittest.main()
