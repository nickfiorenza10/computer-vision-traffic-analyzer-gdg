import { describe, expect, it } from 'vitest';
import { binRows, classTable, directionKind, laneBars, summarize } from './aggregate';
import type { ClassCount, LaneTotal, TimeBin } from '../types';

// The sample camera's lane totals, from T2's queries.sql output.
const totals: LaneTotal[] = [
  { lane: 1, direction: 'inbound', vehicle_count: 42 },
  { lane: 2, direction: 'inbound', vehicle_count: 135 },
  { lane: 3, direction: 'inbound', vehicle_count: 191 },
  { lane: 4, direction: 'outbound', vehicle_count: 158 },
  { lane: 5, direction: 'outbound', vehicle_count: 145 },
  { lane: 6, direction: 'outbound', vehicle_count: 116 },
];

describe('directionKind', () => {
  it('recognises the configurable labels', () => {
    expect(directionKind('inbound')).toBe('inbound');
    expect(directionKind('outbound')).toBe('outbound');
    expect(directionKind('in')).toBe('inbound');
    expect(directionKind('out')).toBe('outbound');
    expect(directionKind('northbound')).toBe('other');
  });
});

describe('summarize', () => {
  it('totals the sample window and finds the busiest lane', () => {
    const summary = summarize(totals);
    expect(summary.total).toBe(787);
    expect(summary.inbound).toBe(368);
    expect(summary.outbound).toBe(419);
    expect(summary.other).toBe(0);
    expect(summary.busiest).toEqual({ lane: 3, count: 191 });
    expect(summary.lanes).toEqual([1, 2, 3, 4, 5, 6]);
    expect(summary.directions).toEqual(['inbound', 'outbound']);
  });

  it('handles an empty window without inventing data', () => {
    expect(summarize([])).toEqual({
      total: 0,
      inbound: 0,
      outbound: 0,
      other: 0,
      lanes: [],
      directions: [],
      busiest: null,
    });
  });
});

describe('laneBars', () => {
  it('splits each lane by direction kind', () => {
    const bars = laneBars(totals);
    expect(bars).toHaveLength(6);
    expect(bars[0]).toMatchObject({ lane: 1, inbound: 42, outbound: 0, total: 42 });
    expect(bars[3]).toMatchObject({ lane: 4, inbound: 0, outbound: 158, total: 158 });
  });
});

describe('binRows', () => {
  const bins: TimeBin[] = [
    { bin_start: '2026-10-09T08:00:00Z', lane: 1, direction: 'inbound', vehicle_count: 18 },
    { bin_start: '2026-10-09T08:00:00Z', lane: 2, direction: 'inbound', vehicle_count: 31 },
    { bin_start: '2026-10-09T08:00:10Z', lane: 1, direction: 'inbound', vehicle_count: 6 },
    // lane 2 has no events in the second bin: that is a gap, not a zero
    { bin_start: '2026-10-09T08:00:20Z', lane: 1, direction: 'inbound', vehicle_count: 4 },
  ];

  it('gives one row per bin across the window', () => {
    const { rows, laneKeys, laneDirectionKeys } = binRows(
      bins,
      '2026-10-09T08:00:00Z',
      '2026-10-09T08:00:30Z',
      10,
    );
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => row.label)).toEqual(['08:00:00', '08:00:10', '08:00:20']);
    expect(laneKeys).toEqual(['lane 1', 'lane 2']);
    expect(laneDirectionKeys).toEqual(['lane 1 · inbound', 'lane 2 · inbound']);
    expect(rows[0]['lane 2 · inbound']).toBe(31);
    expect(rows[1]['lane 2 · inbound']).toBeUndefined();
    expect(rows[2]['lane 2 · inbound']).toBeUndefined();
  });

  it('aligns rows to the bin grid when the window starts mid-bin', () => {
    const { rows } = binRows(
      [
        { bin_start: '2026-10-09T08:00:00Z', lane: 1, direction: 'inbound', vehicle_count: 9 },
        { bin_start: '2026-10-09T08:00:10Z', lane: 1, direction: 'inbound', vehicle_count: 4 },
      ],
      '2026-10-09T08:00:05Z',
      '2026-10-09T08:00:25Z',
      10,
    );
    // Rows are the epoch-aligned bins: :00 (partial, before the window), :10, :20.
    expect(rows.map((row) => row.label)).toEqual(['08:00:00', '08:00:10', '08:00:20']);
    expect(rows[0]['lane 1']).toBe(9);
    expect(rows[1]['lane 1']).toBe(4);
    expect(rows[2]['lane 1']).toBeUndefined();
  });

  it('flags the bins that cover less than a full bin of the window', () => {
    // A window that starts and ends inside a bin: the first and last rows are partial.
    const { rows } = binRows([], '2026-10-09T08:00:05Z', '2026-10-09T08:00:25Z', 10);
    expect(rows.map((row) => row.partial)).toEqual([true, false, true]);

    // The sample camera's real window: 08:00:00.166 to 08:00:42.509 at 10 s.
    const sample = binRows([], '2026-10-09T08:00:00.166Z', '2026-10-09T08:00:43.509Z', 10);
    expect(sample.rows.map((row) => row.partial)).toEqual([true, false, false, false, true]);
    expect(sample.rows[sample.rows.length - 1].label).toBe('08:00:40');
  });

  it('marks no row partial when the window sits exactly on bin boundaries', () => {
    const { rows } = binRows([], '2026-10-09T08:00:00Z', '2026-10-09T08:00:30Z', 10);
    expect(rows.map((row) => row.partial)).toEqual([false, false, false]);
  });

  it('returns nothing for an unusable window', () => {
    expect(binRows(bins, '2026-10-09T08:01:00Z', '2026-10-09T08:00:00Z', 10)).toEqual({
      rows: [],
      laneKeys: [],
      laneDirectionKeys: [],
    });
  });
});

describe('classTable', () => {
  const classes: ClassCount[] = [
    { lane: 3, direction: 'inbound', vehicle_class: 'truck', vehicle_count: 4 },
    { lane: 3, direction: 'inbound', vehicle_class: 'car', vehicle_count: 187 },
    { lane: 5, direction: 'outbound', vehicle_class: 'bus', vehicle_count: 1 },
    { lane: 5, direction: 'outbound', vehicle_class: 'car', vehicle_count: 133 },
  ];

  it('orders classes the way the pipeline lists them', () => {
    const table = classTable(classes);
    expect(table.classes).toEqual(['car', 'truck', 'bus']);
    expect(table.rows[0]).toMatchObject({ lane: 3, total: 191 });
    expect(table.rows[0].counts).toEqual({ car: 187, truck: 4 });
  });

  it('leaves classes that never appear out of the table', () => {
    expect(classTable(classes).classes).not.toContain('motorcycle');
  });
});
