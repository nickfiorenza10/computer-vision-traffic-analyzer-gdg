import { describe, expect, it } from 'vitest';
import { RESULTS_FORMAT, boxesAtFrame, countsAtFrame, frameIndexAt, parseResults, type BoxRow } from './results';

const valid = {
  format: RESULTS_FORMAT,
  video: 'vehicles-2.mp4',
  fps: 30,
  frame_size: [1920, 1080],
  frames_total: 1260,
  start: '2026-10-09T08:00:00',
  classes: ['bicycle', 'car', 'motorcycle', 'bus', 'truck'],
  lanes: {
    frame_size: [3840, 2160],
    lanes: [
      { id: 1, polygon: [[0, 0], [10, 0], [10, 10]], line: [[0, 5], [10, 5]], directions: { in: 'inbound', out: 'outbound' } },
    ],
  },
  events: [
    { timestamp: '2026-10-09T08:00:00.166', frame: 5, track_id: 7, class: 'car', lane: 1, direction: 'outbound' },
    { timestamp: '2026-10-09T08:00:01.000', frame: 30, track_id: 9, class: 'truck', lane: 2, direction: 'inbound' },
  ],
  track_classes: { '7': 'car', '9': 'truck' },
  boxes_every: 3,
  boxes: [
    [0, [[7, 1, 91, 10, 20, 30, 40] as BoxRow]],
    [3, [[7, 1, 90, 12, 22, 32, 42] as BoxRow]],
    [6, [[9, 4, 88, 50, 60, 70, 80] as BoxRow]],
  ],
};

describe('parseResults', () => {
  it('accepts the pipeline format', () => {
    const parsed = parseResults(JSON.stringify(valid));
    expect(parsed.fps).toBe(30);
    expect(parsed.lanes.lanes).toHaveLength(1);
    expect(parsed.boxes_every).toBe(3);
  });

  it('rejects a different format string', () => {
    expect(() => parseResults(JSON.stringify({ ...valid, format: 'traffic-analyzer-results/2' }))).toThrow(
      /Unsupported results format/,
    );
  });

  it('rejects non-JSON and non-object payloads', () => {
    expect(() => parseResults('not json')).toThrow(/not JSON/);
    expect(() => parseResults('[]')).toThrow(/not a results object/);
  });

  it('rejects files without fps, frame_size, lanes, events, boxes or boxes_every', () => {
    for (const key of ['fps', 'frame_size', 'lanes', 'events', 'boxes', 'boxes_every', 'classes'] as const) {
      const broken: Record<string, unknown> = { ...valid };
      delete broken[key];
      expect(() => parseResults(JSON.stringify(broken))).toThrowError();
    }
  });
});

describe('frameIndexAt', () => {
  it('floors the playback position into a frame index', () => {
    expect(frameIndexAt(0, 30)).toBe(0);
    expect(frameIndexAt(0.99, 30)).toBe(29);
    expect(frameIndexAt(1, 30)).toBe(30);
    expect(frameIndexAt(-1, 30)).toBe(0);
  });
});

describe('boxesAtFrame', () => {
  const boxes = valid.boxes as [number, BoxRow[]][];

  it('returns the nearest earlier entry when boxes_every is more than 1', () => {
    expect(boxesAtFrame(boxes, 0)[0][0]).toBe(7);
    expect(boxesAtFrame(boxes, 2)[0][0]).toBe(7);
    expect(boxesAtFrame(boxes, 3)[0][0]).toBe(7);
    expect(boxesAtFrame(boxes, 5)[0][0]).toBe(7);
    expect(boxesAtFrame(boxes, 6)[0][0]).toBe(9);
    expect(boxesAtFrame(boxes, 999)[0][0]).toBe(9);
  });

  it('returns nothing before the first stored frame or for an empty file', () => {
    expect(boxesAtFrame([[10, []]], 9)).toEqual([]);
    expect(boxesAtFrame([], 5)).toEqual([]);
  });
});

describe('countsAtFrame', () => {
  it('counts only events at or before the frame, per lane and direction', () => {
    const events = valid.events;
    expect(countsAtFrame(events, 4)).toEqual({});
    expect(countsAtFrame(events, 5)).toEqual({ '1 outbound': 1 });
    expect(countsAtFrame(events, 30)).toEqual({ '1 outbound': 1, '2 inbound': 1 });
  });
});
