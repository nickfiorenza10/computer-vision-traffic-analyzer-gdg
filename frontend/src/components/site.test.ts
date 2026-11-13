import { describe, expect, it } from 'vitest';

import { countyLevelLabel } from './site';

describe('countyLevelLabel', () => {
  it('speaks for every camera in the county', () => {
    expect(countyLevelLabel('learning', 11)).toBe('Learning these cameras');
    expect(countyLevelLabel('learning', 1)).toBe('Learning this camera');
    expect(countyLevelLabel('no_data', 0)).toBe('No cameras swept');
    expect(countyLevelLabel('heavy', 4)).toBe('Heavy traffic');
    expect(countyLevelLabel('unknown', 2)).toBe('No reading: camera dark in daylight');
  });
});
