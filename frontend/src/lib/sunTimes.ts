/**
 * Sunrise and sunset at a camera, for the panel's "Sunset 6:09 PM" line.
 *
 * A port of `pipeline/daylight.py` (the sunrise equation, within a minute of the US Naval
 * Observatory at these latitudes). The worker uses the Python one to choose day or night
 * detection; this one only labels the panel, so both read the same sun.
 */

const J2000 = 2451545.0;
const UNIX_EPOCH_JD = 2440587.5;
const DAY_MS = 86_400_000;
const OBLIQUITY = (23.4397 * Math.PI) / 180;
const SUNRISE_ALTITUDE = -0.833;
const RAD = Math.PI / 180;

export interface SunTimes {
  sunrise: Date | null;
  sunset: Date | null;
}

/** Sunrise and sunset for the local calendar day `ymd` ("2026-10-29") at a place (lon east). */
export function sunTimes(lat: number, lon: number, ymd: string): SunTimes {
  const noonUtc = Date.parse(`${ymd}T12:00:00Z`);
  const n = Math.round(noonUtc / DAY_MS + UNIX_EPOCH_JD - J2000 - lon / 360);
  const meanNoon = n - lon / 360;
  const anomaly = (((357.5291 + 0.98560028 * meanNoon) % 360) + 360) % 360;
  const m = anomaly * RAD;
  const centre = 1.9148 * Math.sin(m) + 0.02 * Math.sin(2 * m) + 0.0003 * Math.sin(3 * m);
  const eclipticLon = (((anomaly + centre + 180 + 102.9372) % 360) * RAD);
  const transit = J2000 + meanNoon + 0.0053 * Math.sin(m) - 0.0069 * Math.sin(2 * eclipticLon);
  const declination = Math.asin(Math.sin(eclipticLon) * Math.sin(OBLIQUITY));
  const phi = lat * RAD;
  const cosHour =
    (Math.sin(SUNRISE_ALTITUDE * RAD) - Math.sin(phi) * Math.sin(declination)) /
    (Math.cos(phi) * Math.cos(declination));
  if (cosHour < -1 || cosHour > 1) return { sunrise: null, sunset: null };
  const half = Math.acos(cosHour) / RAD / 360;
  const toDate = (jd: number) => new Date((jd - UNIX_EPOCH_JD) * DAY_MS);
  return { sunrise: toDate(transit - half), sunset: toDate(transit + half) };
}

/** California cameras show Pacific time, Florida cameras Eastern. */
export function zoneFor(lon: number): string {
  return lon > -90 ? 'America/New_York' : 'America/Los_Angeles';
}

function ymdIn(zone: string, when: Date): string {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat('en-CA', { timeZone: zone }).format(when);
}

/** Rounded to the nearest minute, as published sun times are (formatting alone truncates). */
function clock(zone: string, when: Date): string {
  const rounded = new Date(Math.round(when.getTime() / 60_000) * 60_000);
  return new Intl.DateTimeFormat('en-US', { timeZone: zone, hour: 'numeric', minute: '2-digit' }).format(rounded);
}

/**
 * The one line for the panel: "Sunset 6:09 PM" while the sun is up, otherwise
 * "Sunrise 7:30 AM" (today's if it is still to come, else tomorrow's). Null if the sun
 * never rises or sets there that day.
 */
export function sunLine(lat: number, lon: number, now: Date = new Date()): string | null {
  const zone = zoneFor(lon);
  const today = sunTimes(lat, lon, ymdIn(zone, now));
  if (!today.sunrise || !today.sunset) return null;
  if (now >= today.sunrise && now < today.sunset) return `Sunset ${clock(zone, today.sunset)}`;
  if (now < today.sunrise) return `Sunrise ${clock(zone, today.sunrise)}`;
  const tomorrow = sunTimes(lat, lon, ymdIn(zone, new Date(now.getTime() + DAY_MS)));
  return tomorrow.sunrise ? `Sunrise ${clock(zone, tomorrow.sunrise)}` : null;
}
