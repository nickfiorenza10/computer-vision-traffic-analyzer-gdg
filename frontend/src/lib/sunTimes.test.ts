import { describe, expect, it } from 'vitest';
import { sunLine, sunTimes, zoneFor } from './sunTimes';

// US Naval Observatory, aa.usno.navy.mil/api/rstt (the same references as pipeline/tests/test_daylight.py).
const SACRAMENTO = [38.5816, -121.4944] as const;
const TAMPA = [27.9506, -82.4572] as const;

function minutesOff(got: Date | null, isoWant: string): number {
  expect(got).not.toBeNull();
  return Math.abs((got as Date).getTime() - Date.parse(isoWant)) / 60_000;
}

describe('sunTimes', () => {
  it('matches the Naval Observatory within two minutes', () => {
    const cases: [readonly [number, number], string, string, string][] = [
      [SACRAMENTO, '2026-10-29', '2026-10-29T07:30:00-07:00', '2026-10-29T18:09:00-07:00'],
      [TAMPA, '2026-10-29', '2026-10-29T07:39:00-04:00', '2026-10-29T18:48:00-04:00'],
      [SACRAMENTO, '2026-06-21', '2026-06-21T05:42:00-07:00', '2026-06-21T20:34:00-07:00'],
      [TAMPA, '2026-12-21', '2026-12-21T07:17:00-05:00', '2026-12-21T17:39:00-05:00'],
    ];
    for (const [[lat, lon], day, rise, set] of cases) {
      const times = sunTimes(lat, lon, day);
      expect(minutesOff(times.sunrise, rise)).toBeLessThan(2);
      expect(minutesOff(times.sunset, set)).toBeLessThan(2);
    }
  });

  it('has no sunrise in a polar night', () => {
    expect(sunTimes(78.2, 15.6, '2026-12-21')).toEqual({ sunrise: null, sunset: null });
  });
});

/** The model is within a minute of the Naval Observatory, so either neighbouring minute is right. */
function oneOf(...lines: string[]) {
  return (got: string | null) => expect(lines).toContain(got);
}

describe('sunLine', () => {
  it('shows the sunset while the sun is up', () => {
    oneOf('Sunset 6:09 PM', 'Sunset 6:10 PM')(sunLine(...SACRAMENTO, new Date('2026-10-29T13:00:00-07:00')));
    oneOf('Sunset 6:48 PM', 'Sunset 6:49 PM')(sunLine(...TAMPA, new Date('2026-10-29T12:00:00-04:00')));
  });

  it("shows this morning's sunrise before dawn and tomorrow's after sunset", () => {
    oneOf('Sunrise 7:29 AM', 'Sunrise 7:30 AM')(sunLine(...SACRAMENTO, new Date('2026-10-29T05:00:00-07:00')));
    // Tomorrow, 2026-10-30, Tampa: 07:39 EDT.
    oneOf('Sunrise 7:39 AM', 'Sunrise 7:40 AM')(sunLine(...TAMPA, new Date('2026-10-29T22:00:00-04:00')));
  });

  it('says nothing where the sun does not rise', () => {
    expect(sunLine(78.2, 15.6, new Date('2026-12-21T12:00:00Z'))).toBeNull();
  });

  it('uses Eastern time for Florida and Pacific for California', () => {
    expect(zoneFor(TAMPA[1])).toBe('America/New_York');
    expect(zoneFor(SACRAMENTO[1])).toBe('America/Los_Angeles');
  });
});
