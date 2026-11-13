/** Automatic time-bin sizing for the volume chart.
 *
 *  The API accepts any `bin_seconds` from 1 to 86400 and at most 5000 bins per
 *  request; this is a display choice, not an API limit. The ladder starts at
 *  10 s and we take the first size that keeps the chart at or under 300 columns,
 *  which gives 10 s bins for a one-minute window and 5 min bins for a day.
 */

export const BIN_LADDER = [
  10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400,
] as const;

export const MAX_BINS = 300;

const MS_PER_SECOND = 1000;

/** Seconds between `start` and `end`, or 0 when either is unparseable. */
export function windowSeconds(start: string, end: string): number {
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return 0;
  return (b - a) / MS_PER_SECOND;
}

export function chooseBinSeconds(start: string, end: string): number {
  const seconds = windowSeconds(start, end);
  if (seconds <= 0) return BIN_LADDER[0];
  for (const size of BIN_LADDER) {
    if (Math.ceil(seconds / size) <= MAX_BINS) return size;
  }
  return BIN_LADDER[BIN_LADDER.length - 1];
}

/** Human label for a bin size: "10 s", "5 min", "2 h". */
export function formatBinSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600 && seconds % 60 === 0) return `${seconds / 60} min`;
  if (seconds % 3600 === 0 && seconds < 86400) return `${seconds / 3600} h`;
  if (seconds === 86400) return '1 day';
  return `${Math.round(seconds)} s`;
}
