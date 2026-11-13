import { describe, expect, it } from 'vitest';

import { LOCAL_ROADS, highwayFilter, highwayGroups, onHighway, parseHighway, withNamedGaps } from './highways';

describe('parseHighway', () => {
  it('reads Caltrans routes', () => {
    expect(parseHighway('I-80')).toEqual({ key: 'I-80', network: 'us-interstate', ref: '80' });
    expect(parseHighway('US-50')).toEqual({ key: 'US-50', network: 'us-highway', ref: '50' });
    expect(parseHighway('SR-99')).toEqual({ key: 'SR-99', network: 'us-state', ref: '99' });
  });

  it('reads FL511 roads and leaves streets alone', () => {
    expect(parseHighway('SR 600/Gandy Blvd')).toEqual({ key: 'SR-600', network: 'us-state', ref: '600', street: 'Gandy' });
    expect(parseHighway('SR 60/Courtney Campbell Causeway')?.street).toBe('Courtney Campbell Causeway');
    expect(parseHighway('I-275')?.key).toBe('I-275');
    expect(parseHighway('Himes Avenue')).toBeNull();
    expect(parseHighway('for City of Tampa cameras')).toBeNull();
    expect(parseHighway('')).toBeNull();
    expect(parseHighway(null)).toBeNull();
  });
});

describe('highwayGroups', () => {
  it('puts interstates first, then US and state routes, busiest first, streets last', () => {
    const groups = highwayGroups(['SR-99', 'US-50', 'I-80', 'US-50', 'Himes Avenue', 'I-5', 'I-5', 'I-5', 'SR-51']);
    expect(groups.map((g) => [g.label, g.count])).toEqual([
      ['I-5', 3],
      ['I-80', 1],
      ['US-50', 2],
      ['SR-51', 1],
      ['SR-99', 1],
      ['Local roads', 1],
    ]);
  });

  it('matches a camera to its group', () => {
    expect(onHighway('US-50', 'US-50')).toBe(true);
    expect(onHighway('I-80', 'US-50')).toBe(false);
    expect(onHighway('Himes Avenue', LOCAL_ROADS)).toBe(true);
  });
});

describe('highwayFilter', () => {
  it('matches nothing without a highway', () => {
    expect(highwayFilter(null)).toEqual(['==', ['literal', 1], 0]);
  });

  it('matches the main route and every route_N slot, with OSM network codes', () => {
    const filter = JSON.stringify(highwayFilter(parseHighway('US-50')));
    expect(filter).toContain('["==",["get","network"],"us-highway"]');
    expect(filter).toContain('["US:US"]');
    expect(filter).toContain('"route_6_ref"');
    expect(JSON.stringify(highwayFilter(parseHighway('SR-99')))).toContain('["US:CA","US:FL"]');
  });

  it('matches SR-51 as the Business 80 it is mapped as, but not where it is US-50', () => {
    const filter = JSON.stringify(highwayFilter(parseHighway('SR-51')));
    expect(filter).toContain('"US:I:Business:Loop"');
    expect(filter).toContain('["!",["in","US 50",');
    expect(JSON.stringify(highwayFilter(parseHighway('SR-99')))).not.toContain('Business');
  });

  it('also matches the street by name when the route names one', () => {
    expect(JSON.stringify(highwayFilter(parseHighway('SR 600/Gandy Blvd')))).toContain('["in","Gandy",');
    expect(JSON.stringify(highwayFilter(parseHighway('I-80')))).not.toContain('"in"');
  });

  it('fills unnumbered stretches that share a name with the matched road', () => {
    const highway = parseHighway('SR-99')!;
    const base = highwayFilter(highway);
    expect(withNamedGaps(base, highway, [])).toBe(base);
    const filled = JSON.stringify(withNamedGaps(base, highway, ['Golden State Highway']));
    expect(filled).toContain('["==",["get","network"],"us-state"]');
    expect(filled).toContain('["Golden State Highway"]');
  });
});
