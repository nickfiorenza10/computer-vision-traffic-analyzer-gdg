import { describe, expect, it } from 'vitest';

import { basemapPaint } from './stateMap';

describe('basemapPaint', () => {
  it('turns the basemap to the dark palette, motorways brightest', () => {
    const line = (id: string) => basemapPaint({ id, type: 'line', 'source-layer': 'transportation' });
    expect(line('road_motorway')).toEqual({ 'line-color': '#4A505A' });
    expect(line('bridge_trunk_primary')).toEqual({ 'line-color': '#4A505A' });
    expect(line('road_secondary_tertiary')).toEqual({ 'line-color': '#2A2E35' });
    expect(line('road_minor')).toEqual({ 'line-color': '#1E2126' });
    expect(line('road_motorway_casing')).toEqual({ 'line-color': '#0B0C0E' });
  });

  it('colours water and leaves other layers alone', () => {
    expect(basemapPaint({ id: 'water', type: 'fill', 'source-layer': 'water' })).toEqual({ 'fill-color': '#101A22' });
    expect(basemapPaint({ id: 'waterway_river', type: 'line', 'source-layer': 'waterway' })).toEqual({ 'line-color': '#16232D' });
    expect(basemapPaint({ id: 'park', type: 'fill', 'source-layer': 'park' })).toBeNull();
  });
});
