import { describe, expect, it } from 'vitest';
import { BIN_LADDER, MAX_BINS, chooseBinSeconds, formatBinSeconds, windowSeconds } from './bins';

describe('chooseBinSeconds', () => {
  it('uses 10 s bins for a one-minute window', () => {
    expect(chooseBinSeconds('2026-10-09T08:00:00Z', '2026-10-09T08:01:00Z')).toBe(10);
  });

  it('uses 5 min bins for a one-day window', () => {
    expect(chooseBinSeconds('2026-10-09T00:00:00Z', '2026-10-10T00:00:00Z')).toBe(300);
  });

  it('never returns more than the display budget of bins', () => {
    const start = Date.parse('2026-10-01T00:00:00Z');
    const end = Date.parse('2026-10-31T23:59:59Z'); // just under the API's 31-day limit
    const size = chooseBinSeconds(new Date(start).toISOString(), new Date(end).toISOString());
    expect(Math.ceil(windowSeconds(new Date(start).toISOString(), new Date(end).toISOString()) / size)).toBeLessThanOrEqual(
      MAX_BINS,
    );
  });

  it('falls back to the first rung for a broken or empty range', () => {
    expect(chooseBinSeconds('nonsense', '2026-10-09T08:01:00Z')).toBe(BIN_LADDER[0]);
    expect(chooseBinSeconds('2026-10-09T08:01:00Z', '2026-10-09T08:00:00Z')).toBe(BIN_LADDER[0]);
  });

  it('clamps to the largest rung past the ladder', () => {
    expect(chooseBinSeconds('2026-01-01T00:00:00Z', '2026-12-31T00:00:00Z')).toBe(BIN_LADDER[BIN_LADDER.length - 1]);
  });
});

describe('formatBinSeconds', () => {
  it('labels seconds, minutes and hours', () => {
    expect(formatBinSeconds(10)).toBe('10 s');
    expect(formatBinSeconds(300)).toBe('5 min');
    expect(formatBinSeconds(1800)).toBe('30 min');
    expect(formatBinSeconds(7200)).toBe('2 h');
    expect(formatBinSeconds(86400)).toBe('1 day');
  });

  it('labels nonsense as a dash', () => {
    expect(formatBinSeconds(0)).toBe('—');
  });
});
