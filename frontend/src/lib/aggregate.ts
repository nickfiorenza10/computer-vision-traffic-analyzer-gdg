/** Turning API rows into what the views draw. Missing bins stay missing: the
 *  API omits groups with no events, and a gap means "no data", never zero. */

import type { ClassCount, LaneTotal, TimeBin } from '../types';

export type DirectionKind = 'inbound' | 'outbound' | 'other';

/** Lane direction labels are configurable per camera ("inbound"/"outbound" or "in"/"out"). */
export function directionKind(direction: string): DirectionKind {
  const value = direction.trim().toLowerCase();
  if (value.startsWith('out')) return 'outbound';
  if (value.startsWith('in')) return 'inbound';
  return 'other';
}

export type Summary = {
  total: number;
  inbound: number;
  outbound: number;
  other: number;
  lanes: number[];
  directions: string[];
  busiest: { lane: number; count: number } | null;
};

export function summarize(totals: LaneTotal[]): Summary {
  const byLane = new Map<number, number>();
  const directions = new Set<string>();
  let inbound = 0;
  let outbound = 0;
  let other = 0;
  let total = 0;

  for (const row of totals) {
    total += row.vehicle_count;
    byLane.set(row.lane, (byLane.get(row.lane) ?? 0) + row.vehicle_count);
    directions.add(row.direction);
    const kind = directionKind(row.direction);
    if (kind === 'inbound') inbound += row.vehicle_count;
    else if (kind === 'outbound') outbound += row.vehicle_count;
    else other += row.vehicle_count;
  }

  let busiest: Summary['busiest'] = null;
  for (const [lane, count] of [...byLane.entries()].sort((a, b) => a[0] - b[0])) {
    if (!busiest || count > busiest.count) busiest = { lane, count };
  }

  return {
    total,
    inbound,
    outbound,
    other,
    lanes: [...byLane.keys()].sort((a, b) => a - b),
    directions: [...directions].sort(),
    busiest,
  };
}

export type LaneBar = {
  lane: number;
  label: string;
  inbound: number;
  outbound: number;
  other: number;
  total: number;
};

/** One bar per lane, split by direction kind. */
export function laneBars(totals: LaneTotal[]): LaneBar[] {
  const byLane = new Map<number, LaneBar>();
  for (const row of totals) {
    const bar =
      byLane.get(row.lane) ??
      { lane: row.lane, label: `Lane ${row.lane}`, inbound: 0, outbound: 0, other: 0, total: 0 };
    const kind = directionKind(row.direction);
    bar[kind] += row.vehicle_count;
    bar.total += row.vehicle_count;
    byLane.set(row.lane, bar);
  }
  return [...byLane.values()].sort((a, b) => a.lane - b.lane);
}

export type BinRow = {
  t: number;
  label: string;
  /** True when the bin starts before the window or runs past its end. */
  partial: boolean;
  [key: string]: number | string | boolean | undefined;
};

const timeFormat = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});

function binLabel(epochMs: number): string {
  return timeFormat.format(new Date(epochMs));
}

/** Lane key used by the stacked chart. */
export const laneKey = (lane: number) => `lane ${lane}`;
/** Lane-and-direction key, used when the chart is split by direction. */
export const laneDirectionKey = (lane: number, direction: string) => `lane ${lane} · ${direction}`;

/**
 * Rows for the volume chart, one per bin across `[start, end)`.
 *
 * Every row carries a `lane N` total and a `lane N · direction` value for each
 * group. A group with no events in that bin is left undefined so the chart draws
 * a gap instead of a zero.
 */
export function binRows(
  bins: TimeBin[],
  start: string,
  end: string,
  binSeconds: number,
): { rows: BinRow[]; laneKeys: string[]; laneDirectionKeys: string[] } {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const step = binSeconds * 1000;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs || step <= 0) {
    return { rows: [], laneKeys: [], laneDirectionKeys: [] };
  }

  // Bins align to the Unix epoch (date_bin), so the row grid must too: a row can
  // start just before the requested window, which is the partial first bin.
  const gridStart = Math.floor(startMs / step) * step;
  const rows: BinRow[] = [];
  const rowCount = Math.ceil((endMs - gridStart) / step);
  for (let index = 0; index < rowCount; index += 1) {
    const t = gridStart + index * step;
    // A bin is partial when it starts before the requested window or runs past its end:
    // its count covers less than `bin_seconds` of footage.
    rows.push({ t, label: binLabel(t), partial: t < startMs || t + step > endMs });
  }

  const laneKeys = new Set<string>();
  const laneDirectionKeys = new Set<string>();

  for (const bin of bins) {
    const binStart = Date.parse(bin.bin_start);
    if (!Number.isFinite(binStart)) continue;
    const index = Math.round((binStart - gridStart) / step);
    const row = rows[index];
    if (!row) continue;

    const lane = laneKey(bin.lane);
    const laneDirection = laneDirectionKey(bin.lane, bin.direction);
    laneKeys.add(lane);
    laneDirectionKeys.add(laneDirection);
    row[lane] = ((row[lane] as number | undefined) ?? 0) + bin.vehicle_count;
    row[laneDirection] = ((row[laneDirection] as number | undefined) ?? 0) + bin.vehicle_count;
  }

  const sortNumeric = (keys: Set<string>) =>
    [...keys].sort((a, b) => {
      const laneOf = (key: string) => Number(key.replace('lane ', '').split(' ')[0]);
      return laneOf(a) - laneOf(b) || a.localeCompare(b);
    });

  return { rows, laneKeys: sortNumeric(laneKeys), laneDirectionKeys: sortNumeric(laneDirectionKeys) };
}

const CLASS_ORDER = ['car', 'truck', 'bus', 'motorcycle', 'bicycle'];

export type ClassTable = {
  classes: string[];
  rows: { lane: number; label: string; counts: Record<string, number>; total: number }[];
};

/** Per-lane class counts, in the pipeline's class order. */
export function classTable(classes: ClassCount[]): ClassTable {
  const present = new Set(classes.map((row) => row.vehicle_class));
  const ordered = CLASS_ORDER.filter((name) => present.has(name));
  for (const name of [...present].sort()) if (!ordered.includes(name)) ordered.push(name);

  const byLane = new Map<number, ClassTable['rows'][number]>();
  for (const row of classes) {
    const entry =
      byLane.get(row.lane) ?? { lane: row.lane, label: `Lane ${row.lane}`, counts: {}, total: 0 };
    entry.counts[row.vehicle_class] = (entry.counts[row.vehicle_class] ?? 0) + row.vehicle_count;
    entry.total += row.vehicle_count;
    byLane.set(row.lane, entry);
  }

  return { classes: ordered, rows: [...byLane.values()].sort((a, b) => a.lane - b.lane) };
}

/** The redesign's accent first, then colours that stay apart from it on the dark ground. */
export const LANE_COLORS = ['#3DDC97', '#7CCBF0', '#FFB224', '#B39DF0', '#FF6369', '#C9CDD3'];

export function laneColor(lane: number): string {
  return LANE_COLORS[(lane - 1) % LANE_COLORS.length];
}

export const CLASS_COLORS: Record<string, string> = {
  car: '#3DDC97',
  truck: '#FFB224',
  bus: '#7CCBF0',
  motorcycle: '#B39DF0',
  bicycle: '#FF6369',
};
