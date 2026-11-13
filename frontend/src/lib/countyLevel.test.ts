import { describe, expect, it } from 'vitest';

import { BUSY_SHARE, HEAVY_SHARE, MAX_SWEEP_AGE_MINUTES, countyLevel } from './countyLevel';
import type { CountyCamera } from './countyLevel';
import type { MapLevel } from '../types';

const NOW = Date.parse('2026-10-29T18:30:00Z');
const TZ = 'America/Los_Angeles';

function fresh(level: MapLevel | null): CountyCamera {
  return { level, sweptAt: '2026-10-29T18:20:00Z' };
}
function stale(level: MapLevel | null): CountyCamera {
  return { level, sweptAt: '2026-10-29T17:00:00Z' };
}
function many(level: MapLevel | null, count: number): CountyCamera[] {
  return Array.from({ length: count }, () => fresh(level));
}

describe('a camera dark in daylight', () => {
  it('is not a reading, and is not night either', () => {
    // T20: the sun was up but the road was dark, so the sweep says "unknown".
    const verdict = countyLevel([...many('unknown', 3), ...many('light', 1)], NOW, TZ);
    expect(verdict.level).toBe('light');
    expect(verdict.readings).toBe(1);
    expect(countyLevel(many('unknown', 2), NOW, TZ).level).toBe('learning');
  });
});

describe('a county with readings', () => {
  it('is heavy at exactly a quarter heavy', () => {
    const verdict = countyLevel([...many('heavy', 1), ...many('light', 3)], NOW, TZ);
    expect(HEAVY_SHARE).toBe(0.25);
    expect(verdict.level).toBe('heavy');
    expect(verdict.reason).toContain('1 of 4 cameras heavy');
  });

  it('is not heavy just under a quarter, and heavy wins over the busy share', () => {
    expect(countyLevel([...many('heavy', 1), ...many('light', 4)], NOW, TZ).level).toBe('light');
    expect(countyLevel([...many('heavy', 3), ...many('moderate', 1)], NOW, TZ).level).toBe('heavy');
  });

  it('is moderate at exactly 40% heavy or moderate', () => {
    const verdict = countyLevel([...many('moderate', 2), ...many('light', 3)], NOW, TZ);
    expect(BUSY_SHARE).toBe(0.4);
    expect(verdict.level).toBe('moderate');
    expect(verdict.reason).toContain('2 of 5 cameras heavy or moderate');
  });

  it('is light when most are light, and says so with the sweep time', () => {
    const verdict = countyLevel([...many('light', 3), ...many('night', 1)], NOW, TZ);
    expect(verdict.level).toBe('light');
    expect(verdict.readings).toBe(3);
    expect(verdict.reason).toMatch(/Light on 3 of 4 cameras · sweep \d{1,2}:\d{2} (AM|PM)/);
  });

  it('ignores night and learning cameras when it counts readings', () => {
    const verdict = countyLevel([...many('light', 1), ...many('night', 9)], NOW, TZ);
    expect(verdict.level).toBe('light');
    expect(verdict.readings).toBe(1);
  });
});

describe('a county with no readings', () => {
  it('is night when most cameras are in the dark', () => {
    const verdict = countyLevel([...many('night', 3), ...many('learning', 1)], NOW, TZ);
    expect(verdict.level).toBe('night');
    expect(verdict.reason).toContain('3 of 4 cameras in the dark');
  });

  it('is learning otherwise', () => {
    expect(countyLevel(many('learning', 4), NOW, TZ).level).toBe('learning');
  });

  it('is no data with no cameras at all', () => {
    const verdict = countyLevel([], NOW, TZ);
    expect(verdict.level).toBe('no_data');
    expect(verdict.reason).toBe('No cameras here yet');
  });
});

describe('stale sweeps', () => {
  it('does not count a sweep older than 45 minutes', () => {
    expect(MAX_SWEEP_AGE_MINUTES).toBe(45);
    const verdict = countyLevel([...many('heavy', 1), stale('light')], NOW, TZ);
    expect(verdict.readings).toBe(1);
    expect(verdict.level).toBe('heavy');
  });

  it('falls back to learning when every sweep is stale', () => {
    const verdict = countyLevel([stale('heavy'), stale('moderate')], NOW, TZ);
    expect(verdict.level).toBe('learning');
    expect(verdict.reason).toContain('No sweep in the last 45 minutes');
  });

  it('treats a camera that has never been swept as no reading', () => {
    const verdict = countyLevel([{ level: null, sweptAt: null }], NOW, TZ);
    expect(verdict.readings).toBe(0);
    expect(verdict.level).toBe('learning');
  });
});
