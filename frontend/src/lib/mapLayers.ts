/* MapLibre draws the cameras; this file owns the layer ids and the styling.
 *
 *  Two modes, two drawings:
 *
 *  - **Traffic level** must show the level of each road, not how many cameras are nearby.
 *    A heatmap sums weight across neighbours, so a hundred `night` cameras glow as hot as a
 *    hundred jams — which is exactly the bug this replaced. Here each camera is a large,
 *    blurred, low-opacity circle in its own level colour, and only cameras with a real
 *    reading are drawn at all.
 *  - **Camera coverage** is about where cameras are, so it keeps the heatmap and every
 *    camera counts the same.
 */

import type {
  CircleLayerSpecification,
  HeatmapLayerSpecification,
  LayerSpecification,
} from 'maplibre-gl';
import type { MapCamerasResponse, MapLevel } from '../types';
import { LEVEL_COLOURS, UNSWEPT_COLOUR, geoJsonFor, type MapMode } from '../lib/map';

export const CAMERAS_SOURCE = 'cameras';
export const HEAT_LAYER = 'camera-heat';
export const GLOW_LAYER = 'camera-glow';
export const POINT_LAYER = 'camera-points';

/** OpenFreeMap's public vector tiles: no key, OSM data, attribution kept in the corner. */
export const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty';

/** The camera source with colours and weights already resolved for the current mode. */
export function cameraSource(cameras: MapCamerasResponse, mode: MapMode) {
  return {
    type: 'geojson' as const,
    data: geoJsonFor(cameras.features, mode) as unknown as Record<string, unknown>,
  };
}

/** Heat for camera coverage: every camera counts the same, so the heat is camera density. */
export const heatLayer: HeatmapLayerSpecification = {
  id: HEAT_LAYER,
  type: 'heatmap',
  source: CAMERAS_SOURCE,
  paint: {
    'heatmap-weight': ['get', 'weight'],
    'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 5, 1, 11, 2.2],
    'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 4, 12, 8, 26, 12, 46],
    // Heat is a whole-district view; past neighbourhood zoom the points say it better, so the
    // heat fades out instead of hanging a halo around a single camera.
    'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 9, 0.7, 12, 0.25, 14, 0],
    'heatmap-color': [
      'interpolate',
      ['linear'],
      ['heatmap-density'],
      0,
      'rgba(0,0,0,0)',
      0.2,
      'rgba(65,171,93,0.55)',
      0.45,
      'rgba(253,141,60,0.75)',
      0.75,
      'rgba(215,48,31,0.9)',
    ],
  },
};

/** One blurred wash per camera that has a traffic reading, in that reading's colour. */
export const glowLayer: CircleLayerSpecification = {
  id: GLOW_LAYER,
  type: 'circle',
  source: CAMERAS_SOURCE,
  filter: ['in', ['get', 'level'], ['literal', ['heavy', 'moderate', 'light']]],
  paint: {
    'circle-color': ['get', 'colour'],
    'circle-blur': 1,
    'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 26, 9, 34, 13, 44],
    'circle-opacity': ['interpolate', ['linear'], ['zoom'], 5, 0.5, 12, 0.38, 15, 0.2],
  },
};

/** Points: the exact camera positions in their own colours, shown from neighbourhood zoom up. */
export const pointLayer: CircleLayerSpecification = {
  id: POINT_LAYER,
  type: 'circle',
  source: CAMERAS_SOURCE,
  minzoom: 7,
  paint: {
    'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 3, 12, 7, 16, 10],
    'circle-color': ['get', 'colour'],
    'circle-stroke-width': 1,
    'circle-stroke-color': '#ffffff',
    'circle-opacity': 0.95,
  },
};

/** Every layer the map needs, added once; the mode only changes which are visible. */
export const ALL_LAYERS: LayerSpecification[] = [heatLayer, glowLayer, pointLayer];

/** Which layers are visible in a mode: one wash, and the points in both. */
export function visibleLayers(mode: MapMode): string[] {
  return mode === 'coverage' ? [HEAT_LAYER, POINT_LAYER] : [GLOW_LAYER, POINT_LAYER];
}

/** The colour for a level, exported here so the legend and the map cannot disagree. */
export function levelColour(level: MapLevel): string {
  return LEVEL_COLOURS[level];
}

export const UNSWEPT = UNSWEPT_COLOUR;
