/** One level per county, from the cameras inside it.
 *
 *  Daksh signs off on these numbers, the same as `pipeline/traffic_level.py`; they are a
 *  product judgement, not a law of traffic. A county is "heavy" when at least a quarter of its
 *  reporting cameras are heavy, "moderate" when at least 40% are heavy or moderate, and
 *  "light" otherwise. Counties with no reporting camera fall back to what their cameras say:
 *  mostly night is "night", anything else is "learning". No cameras at all is "no data".
 */

import type { MapLevel } from '../types';

/** A camera whose last sweep is older than this is not a reading. */
export const MAX_SWEEP_AGE_MINUTES = 45;
/** Share of reporting cameras that must be heavy for the county to be heavy. */
export const HEAVY_SHARE = 0.25;
/** Share that must be heavy or moderate for the county to be moderate. */
export const BUSY_SHARE = 0.4;
/** Share of all cameras saying "night" that makes an unread county "night". */
export const NIGHT_SHARE = 0.5;

export type CountyCamera = {
  /** heavy / moderate / light are readings; night and learning are not. */
  level: MapLevel | null;
  /** When the last sweep finished, ISO 8601, or null if never swept. */
  sweptAt: string | null;
};

export type CountyVerdict = {
  level: MapLevel | 'no_data';
  /** The line under the level box: "3 of 9 cameras heavy · sweep 7:30 AM". */
  reason: string;
  /** Cameras with a reading that the verdict used. */
  readings: number;
  /** Cameras in the county. */
  cameras: number;
};

export const READING_LEVELS: MapLevel[] = ['heavy', 'moderate', 'light'];

export function hasReading(level: MapLevel | null): boolean {
  return level !== null && READING_LEVELS.includes(level);
}

/** Is this camera's sweep young enough to count? */
export function isFresh(sweptAt: string | null, now: number, maxAgeMinutes = MAX_SWEEP_AGE_MINUTES): boolean {
  if (!sweptAt) return false;
  const at = Date.parse(sweptAt);
  if (!Number.isFinite(at)) return false;
  return now - at <= maxAgeMinutes * 60_000;
}

function share(part: number, whole: number): number {
  return whole === 0 ? 0 : part / whole;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

/** "7:30 AM" in the camera's county timezone, or "" when there is no sweep to name. */
export function clockLabel(sweptAt: string | null, timeZone: string): string {
  if (!sweptAt) return '';
  const at = Date.parse(sweptAt);
  if (!Number.isFinite(at)) return '';
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone,
  }).format(new Date(at));
}

/**
 * The county's level. `now` is passed in so tests never read the clock.
 */
export function countyLevel(
  cameras: CountyCamera[],
  now: number,
  timeZone: string,
): CountyVerdict {
  const total = cameras.length;
  if (total === 0) {
    return { level: 'no_data', reason: 'No cameras here yet', readings: 0, cameras: 0 };
  }

  const fresh = cameras.filter((camera) => isFresh(camera.sweptAt, now));
  const reporting = fresh.filter((camera) => hasReading(camera.level));
  const heavy = reporting.filter((camera) => camera.level === 'heavy').length;
  const busy = reporting.filter((camera) => camera.level === 'heavy' || camera.level === 'moderate').length;
  const readings = reporting.length;

  // The newest sweep among the cameras that reported, for the reason line.
  const newest = reporting
    .map((camera) => camera.sweptAt as string)
    .sort()
    .at(-1) ?? null;
  const sweep = newest ? `sweep ${clockLabel(newest, timeZone)}` : '';

  if (readings > 0) {
    if (share(heavy, readings) >= HEAVY_SHARE) {
      return {
        level: 'heavy',
        reason: `${heavy} of ${plural(readings, 'camera')} heavy${sweep ? ` · ${sweep}` : ''}`,
        readings,
        cameras: total,
      };
    }
    if (share(busy, readings) >= BUSY_SHARE) {
      return {
        level: 'moderate',
        reason: `${busy} of ${plural(readings, 'camera')} heavy or moderate${sweep ? ` · ${sweep}` : ''}`,
        readings,
        cameras: total,
      };
    }
    return {
      level: 'light',
      reason: `Light on ${readings} of ${plural(total, 'camera')}${sweep ? ` · ${sweep}` : ''}`,
      readings,
      cameras: total,
    };
  }

  // Nothing with a reading: say what the cameras do say.
  const night = cameras.filter((camera) => camera.level === 'night').length;
  if (share(night, total) >= NIGHT_SHARE) {
    return {
      level: 'night',
      reason: `${night} of ${plural(total, 'camera')} in the dark · no daylight reading`,
      readings: 0,
      cameras: total,
    };
  }
  const stale = cameras.length - fresh.length;
  return {
    level: 'learning',
    reason: stale > 0
      ? `No sweep in the last ${MAX_SWEEP_AGE_MINUTES} minutes`
      : `Learning ${plural(total, 'camera')} · first reading after 8 sweeps`,
    readings: 0,
    cameras: total,
  };
}
