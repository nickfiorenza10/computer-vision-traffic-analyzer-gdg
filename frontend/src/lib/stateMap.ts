/** Colour and shape helpers for the county map. Pure, so the rules can be tested. */

import type { CountyFeature } from './counties';
import type { CountyVerdict } from './countyLevel';
import { LOCAL_ROADS, parseHighway } from './highways';
import type { MapCameraFeature, MapLevel } from '../types';

/** The redesign's palette. Level colours also live in styles.css; these feed MapLibre. */
export const LEVEL_COLOURS: Record<MapLevel, string> = {
  heavy: '#FF6369',
  moderate: '#FFB224',
  light: '#2F8F63',
  learning: '#6E6AB0',
  night: '#343A46',
  unknown: '#191C21',
};

export const NO_DATA_COLOUR = '#191C21';
export const COVERAGE_RAMP = [
  { limit: 4, colour: '#1D3B4D', label: '1–4' },
  { limit: 20, colour: '#24607F', label: '5–20' },
  { limit: 50, colour: '#3390BD', label: '21–50' },
  { limit: Number.POSITIVE_INFINITY, colour: '#7CCBF0', label: '51+' },
] as const;

export type MapMode = 'level' | 'coverage';

export function coverageColour(cameras: number): string {
  if (cameras <= 0) return NO_DATA_COLOUR;
  return (COVERAGE_RAMP.find((step) => cameras <= step.limit) ?? COVERAGE_RAMP[3]).colour;
}

export type CountyDatum = {
  name: string;
  cameras: number;
  verdict: CountyVerdict;
};

/** One colour per county: its level, or how many cameras it has. */
export function countyFill(datum: CountyDatum, mode: MapMode): string {
  if (mode === 'coverage') return coverageColour(datum.cameras);
  return datum.verdict.level === 'no_data' ? NO_DATA_COLOUR : LEVEL_COLOURS[datum.verdict.level];
}

export type CountyGeo = {
  type: 'FeatureCollection';
  features: {
    type: 'Feature';
    properties: { name: string; fips: string; fill: string; cameras: number; level: string };
    geometry: CountyFeature['geometry'];
  }[];
};

export function countiesGeoJson(
  counties: CountyFeature[],
  data: Map<string, CountyDatum>,
  mode: MapMode,
): CountyGeo {
  return {
    type: 'FeatureCollection',
    features: counties.map((county) => {
      const datum = data.get(county.properties.name);
      const cameras = datum?.cameras ?? 0;
      return {
        type: 'Feature',
        properties: {
          name: county.properties.name,
          fips: county.properties.fips,
          cameras,
          level: datum?.verdict.level ?? 'no_data',
          fill: datum ? countyFill(datum, mode) : NO_DATA_COLOUR,
        },
        geometry: county.geometry,
      };
    }),
  };
}

export type DotGeo = {
  type: 'FeatureCollection';
  features: {
    type: 'Feature';
    /** `highway` is the group key from `highways.ts` ("I-80", or "local" for a street). */
    properties: { camera_id: string; name: string; has_stream: boolean; highway: string };
    geometry: { type: 'Point'; coordinates: [number, number] };
  }[];
};

export function camerasGeoJson(cameras: MapCameraFeature[]): DotGeo {
  return {
    type: 'FeatureCollection',
    features: cameras.map((camera) => ({
      type: 'Feature',
      properties: {
        camera_id: camera.properties.camera_id,
        name: camera.properties.name,
        has_stream: camera.properties.has_stream,
        highway: parseHighway(camera.properties.route)?.key ?? LOCAL_ROADS,
      },
      geometry: { type: 'Point', coordinates: camera.geometry.coordinates },
    })),
  };
}

/** Bounds for a set of coordinates, padded a little so shapes are not flush with the frame. */
export function boundsFor(
  coordinates: [number, number][],
  pad = 0.4,
): [[number, number], [number, number]] | null {
  if (coordinates.length === 0) return null;
  let west = Number.POSITIVE_INFINITY;
  let south = Number.POSITIVE_INFINITY;
  let east = Number.NEGATIVE_INFINITY;
  let north = Number.NEGATIVE_INFINITY;
  for (const [longitude, latitude] of coordinates) {
    west = Math.min(west, longitude);
    east = Math.max(east, longitude);
    south = Math.min(south, latitude);
    north = Math.max(north, latitude);
  }
  return [
    [west - pad, south - pad],
    [east + pad, north + pad],
  ];
}

/** Every coordinate in a set of county shapes, for bounds. */
export function countyCoordinates(counties: CountyFeature[]): [number, number][] {
  const points: [number, number][] = [];
  for (const county of counties) {
    const groups = county.geometry.type === 'Polygon'
      ? [county.geometry.coordinates]
      : county.geometry.coordinates;
    for (const rings of groups) {
      for (const ring of rings) {
        for (const point of ring) points.push([point[0], point[1]]);
      }
    }
  }
  return points;
}

export const BASEMAP_MIN_ZOOM = 9;

/** The map style: no basemap below zoom 9, just the panel colour, so counties carry the page. */
export function mapStyle(): object {
  return {
    version: 8,
    glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
    sources: {},
    layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#101215' } }],
  };
}

/** OpenFreeMap tiles, dimmed, added from zoom 9 up. */
export const BASEMAP_TILES =
  'https://tiles.openfreemap.org/styles/liberty#9.0/38.5/-121.5';

/**
 * The redesign's colours for a basemap layer from OpenFreeMap's Liberty style, which brings
 * its own tan motorways and blue water. Returned as the paint keys to override, or null to
 * leave the layer alone. Opacity is the caller's (it fades the basemap in by zoom).
 */
export function basemapPaint(layer: { id: string; type: string; 'source-layer'?: string }): Record<string, string> | null {
  const sourceLayer = layer['source-layer'];
  if (sourceLayer === 'water') return layer.type === 'fill' ? { 'fill-color': '#101A22' } : null;
  if (sourceLayer === 'waterway') return layer.type === 'line' ? { 'line-color': '#16232D' } : null;
  if (sourceLayer !== 'transportation') return null;
  if (layer.type === 'fill') return { 'fill-color': '#1E2126' };
  if (layer.type !== 'line') return null;
  const id = layer.id.toLowerCase();
  if (id.includes('casing')) return { 'line-color': '#0B0C0E' };
  if (/motorway|trunk/.test(id)) return { 'line-color': '#4A505A' };
  if (/primary|secondary|tertiary/.test(id)) return { 'line-color': '#2A2E35' };
  return { 'line-color': '#1E2126' };
}
