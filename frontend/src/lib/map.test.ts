import { describe, expect, it } from 'vitest';
import {
  UNSWEPT_COLOUR,
  UNSWEPT_WEIGHT,
  anyTrafficReadings,
  colourFor,
  geoJsonFor,
  hasTrafficReading,
  headlineFor,
  labelFor,
  pointsFrom,
  sweepAge,
  tooltipLines,
  weightFor,
} from './map';
import type { MapCameraFeature } from '../types';

function feature(
  cameraId: string,
  level: MapCameraFeature['properties']['level'],
  overrides: Partial<MapCameraFeature['properties']> = {},
): MapCameraFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [-121.5, 38.5] },
    properties: {
      camera_id: cameraId,
      name: `Camera ${cameraId}`,
      county: 'Sacramento',
      route: 'I-5',
      direction: 'Northbound',
      has_stream: true,
      image_url: 'https://example.test/still.jpg',
      swept: level !== null,
      level,
      vehicles_mean: level ? 4.2 : null,
      swept_at: level ? '2026-10-29T12:00:00Z' : null,
      ...overrides,
    },
  };
}

describe('levels on the map', () => {
  it('gives every level its own colour and a camera without a sweep the grey', () => {
    expect(colourFor('heavy')).not.toBe(colourFor('light'));
    expect(colourFor(null)).toBe(UNSWEPT_COLOUR);
  });

  it('weights heat by level in traffic mode and equally in coverage mode', () => {
    expect(weightFor(feature('a', 'heavy').properties, 'level')).toBeGreaterThan(
      weightFor(feature('b', 'light').properties, 'level'),
    );
    expect(weightFor(feature('c', null).properties, 'level')).toBe(UNSWEPT_WEIGHT);
    expect(weightFor(feature('a', 'heavy').properties, 'coverage')).toBe(1);
    expect(weightFor(feature('c', null).properties, 'coverage')).toBe(1);
  });

  it('labels an unswept camera honestly', () => {
    expect(labelFor(null)).toBe('Not swept yet');
    expect(labelFor('learning')).toMatch(/learning/i);
  });
});

describe('GeoJSON for the layers', () => {
  it('bakes the colour and the weight into every feature', () => {
    const features = [feature('a', 'heavy'), feature('b', null)];
    const geo = geoJsonFor(features, 'level');
    expect(geo.type).toBe('FeatureCollection');
    expect(geo.features).toHaveLength(2);
    expect(geo.features[0].properties.weight).toBe(3);
    expect(geo.features[0].properties.colour).toBe(colourFor('heavy'));
    expect(geo.features[0].properties.level).toBe('heavy');
    expect(geo.features[1].properties.level).toBe('none');
    expect(geo.features[1].properties.weight).toBe(0);
  });

  it('keeps the coordinates in lng,lat order', () => {
    const geo = geoJsonFor([feature('a', 'light')], 'coverage');
    expect(geo.features[0].geometry.coordinates).toEqual([-121.5, 38.5]);
  });

  it('flattens points with the camera id, its level and whether it has a stream', () => {
    const points = pointsFrom([feature('a', 'heavy'), feature('b', 'night', { has_stream: false })], 'level');
    expect(points[0]).toMatchObject({ id: 'a', level: 'heavy', hasStream: true, weight: 3 });
    expect(points[1]).toMatchObject({ id: 'b', level: 'night', hasStream: false });
  });

  it('gives a night or learning camera no traffic weight at all', () => {
    // The bug this replaced: a hundred night cameras in Sacramento glowed solid red on US-50
    // while the legend said zero heavy. Night and learning are not traffic readings.
    expect(weightFor(feature('n', 'night').properties, 'level')).toBe(0);
    expect(weightFor(feature('l', 'learning').properties, 'level')).toBe(0);
    expect(weightFor(feature('u', null).properties, 'level')).toBe(0);
    // They are still cameras, so coverage mode still counts them.
    expect(weightFor(feature('n', 'night').properties, 'coverage')).toBe(1);
  });
});

describe('what counts as a traffic reading', () => {
  const many = (level: MapCameraFeature['properties']['level'], count = 50) =>
    Array.from({ length: count }, (_unused, index) => feature(`c${index}`, level));

  it('a cluster of night and learning cameras produces no traffic weight', () => {
    const cluster = [...many('night', 25), ...many('learning', 25)];
    const weights = cluster.map((entry) => weightFor(entry.properties, 'level'));
    expect(Math.max(...weights)).toBe(0);
    expect(anyTrafficReadings(cluster)).toBe(false);
  });

  it('fifty light cameras are light, never heavy', () => {
    const cluster = many('light');
    const weights = cluster.map((entry) => weightFor(entry.properties, 'level'));
    // Every camera keeps the light weight and the light colour: the traffic wash is drawn
    // per camera and never summed into a heat density, so proximity cannot make it heavy.
    expect(new Set(weights)).toEqual(new Set([1]));
    expect(new Set(cluster.map((entry) => colourFor(entry.properties.level)))).toEqual(
      new Set([colourFor('light')]),
    );
    expect(anyTrafficReadings(cluster)).toBe(true);
  });

  it('knows when nothing on the map has a reading, which is when the note shows', () => {
    expect(hasTrafficReading('heavy')).toBe(true);
    expect(hasTrafficReading('moderate')).toBe(true);
    expect(hasTrafficReading('light')).toBe(true);
    expect(hasTrafficReading('learning')).toBe(false);
    expect(hasTrafficReading('night')).toBe(false);
    expect(hasTrafficReading(null)).toBe(false);
    expect(anyTrafficReadings([...many('night', 3), ...many('learning', 3)])).toBe(false);
  });
});

describe('tooltips and the panel headline', () => {
  const now = Date.parse('2026-10-29T12:12:00Z');

  it('says how long ago the sweep ran', () => {
    const properties = feature('a', 'heavy', { swept_at: '2026-10-29T12:00:00Z' }).properties;
    expect(sweepAge(properties, now)).toBe('swept 12 min ago');
  });

  it('handles hours, days and never swept', () => {
    expect(sweepAge(feature('a', 'heavy', { swept_at: '2026-10-29T09:12:00Z' }).properties, now)).toBe(
      'swept 3 h ago',
    );
    expect(sweepAge(feature('a', 'heavy', { swept_at: '2026-10-27T12:12:00Z' }).properties, now)).toBe(
      'swept 2 d ago',
    );
    expect(sweepAge(feature('b', null).properties, now)).toBe('not swept yet');
  });

  it('builds the tooltip: name, where, level, count and sweep age', () => {
    const lines = tooltipLines(feature('a', 'heavy').properties, now);
    expect(lines[0]).toBe('Camera a');
    expect(lines[1]).toBe('I-5 · Northbound');
    expect(lines[2]).toBe('Heavy traffic');
    expect(lines[3]).toBe('4.2 vehicles per still');
    expect(lines[4]).toBe('swept 12 min ago');
  });

  it('keeps the verdict distinct from the coverage wording', () => {
    expect(headlineFor('heavy')).toBe('Heavy traffic');
    expect(headlineFor(null)).toBe('No sweep yet');
    expect(headlineFor('night')).toMatch(/night/i);
  });
});
