/** Everything the map needs that is not map code: level colours, heat weights, labels.
 *
 *  Pure functions, so the mapping from the API's GeoJSON to what MapLibre draws can be
 *  tested without a browser or a tile server.
 */

import type { MapCameraFeature, MapCameraProperties, MapLevel } from '../types';

export type MapMode = 'level' | 'coverage';

/** The five labels the API can send, plus "no sweep yet" (level null). */
export const LEVEL_COLOURS: Record<MapLevel, string> = {
  heavy: '#d7301f',
  moderate: '#fd8d3c',
  light: '#41ab5d',
  learning: '#9e9ac8',
  night: '#4a5568',
  unknown: '#9aa5b1',
};

export const LEVEL_LABELS: Record<MapLevel, string> = {
  heavy: 'Heavy traffic',
  moderate: 'Moderate traffic',
  light: 'Light traffic',
  learning: 'Learning this camera',
  night: 'Night: no daylight result',
  unknown: 'No reading: camera dark in daylight',
};

export const UNSWEPT_COLOUR = '#9aa5b1';

/** Traffic weight per level. Only a real reading counts: `learning`, `night` and a camera
 *  with no sweep are not traffic, and weighting them is what made a cluster of night cameras
 *  glow red on US-50.  */
export const LEVEL_WEIGHTS: Record<MapLevel, number> = {
  heavy: 3,
  moderate: 2,
  light: 1,
  learning: 0,
  night: 0,
  unknown: 0,
};

/** The levels that are a traffic measurement; the rest are the absence of one. */
export const TRAFFIC_LEVELS: MapLevel[] = ['heavy', 'moderate', 'light'];

export const UNSWEPT_WEIGHT = 0;

export function colourFor(level: MapLevel | null): string {
  return level ? LEVEL_COLOURS[level] : UNSWEPT_COLOUR;
}

export function labelFor(level: MapLevel | null): string {
  return level ? LEVEL_LABELS[level] : 'Not swept yet';
}

export function weightFor(properties: MapCameraProperties, mode: MapMode): number {
  if (mode === 'coverage') {
    // Camera coverage is about where cameras are, so every camera counts the same.
    return 1;
  }
  return properties.level ? LEVEL_WEIGHTS[properties.level] : UNSWEPT_WEIGHT;
}

/** Whether this camera has an actual traffic measurement behind it. */
export function hasTrafficReading(level: MapLevel | null): boolean {
  return level !== null && (TRAFFIC_LEVELS as string[]).includes(level);
}

/** Whether any camera on the map has a traffic reading, which decides the "nothing yet" note. */
export function anyTrafficReadings(features: MapCameraFeature[]): boolean {
  return features.some((feature) => hasTrafficReading(feature.properties.level));
}

export type MapPoint = {
  id: string;
  longitude: number;
  latitude: number;
  level: MapLevel | null;
  colour: string;
  weight: number;
  hasStream: boolean;
};

/** The features as MapLibre wants them: a colour and a weight baked in per mode. */
export function pointsFrom(features: MapCameraFeature[], mode: MapMode): MapPoint[] {
  return features.map((feature) => {
    const [longitude, latitude] = feature.geometry.coordinates;
    return {
      id: feature.properties.camera_id,
      longitude,
      latitude,
      level: feature.properties.level,
      colour: colourFor(feature.properties.level),
      weight: weightFor(feature.properties, mode),
      hasStream: feature.properties.has_stream,
    };
  });
}

/** GeoJSON for the heat and circle layers, with the weight and colour already resolved. */
export function geoJsonFor(features: MapCameraFeature[], mode: MapMode): {
  type: 'FeatureCollection';
  features: {
    type: 'Feature';
    geometry: { type: 'Point'; coordinates: [number, number] };
    properties: { id: string; colour: string; weight: number; level: string };
  }[];
} {
  return {
    type: 'FeatureCollection',
    features: features.map((feature) => ({
      type: 'Feature' as const,
      geometry: feature.geometry,
      properties: {
        id: feature.properties.camera_id,
        colour: colourFor(feature.properties.level),
        weight: weightFor(feature.properties, mode),
        level: feature.properties.level ?? 'none',
      },
    })),
  };
}

/** "swept 12 min ago", or that there is no sweep at all. */
export function sweepAge(properties: MapCameraProperties, now: number = Date.now()): string {
  if (!properties.swept || !properties.swept_at) return 'not swept yet';
  const at = Date.parse(properties.swept_at);
  if (!Number.isFinite(at)) return 'not swept yet';
  const minutes = Math.max(0, Math.round((now - at) / 60000));
  if (minutes < 1) return 'swept just now';
  if (minutes < 60) return `swept ${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `swept ${hours} h ago` : `swept ${Math.round(hours / 24)} d ago`;
}

/** The lines a tooltip shows: what the camera is, and what the sweep last said. */
export function tooltipLines(
  properties: MapCameraProperties,
  now: number = Date.now(),
): string[] {
  const where = [properties.route, properties.direction].filter(Boolean).join(' · ');
  const lines = [properties.name || properties.camera_id];
  if (where) lines.push(where);
  lines.push(labelFor(properties.level));
  if (properties.vehicles_mean !== null && properties.level) {
    lines.push(`${properties.vehicles_mean.toFixed(1)} vehicles per still`);
  }
  lines.push(sweepAge(properties, now));
  return lines;
}

/** The verdict in the side panel; deliberately not "heavy" for a camera still learning. */
export function headlineFor(level: MapLevel | null): string {
  switch (level) {
    case 'heavy':
      return 'Heavy traffic';
    case 'moderate':
      return 'Moderate traffic';
    case 'light':
      return 'Light traffic';
    case 'learning':
      return 'Still learning this camera';
    case 'night':
      return 'Night: no daylight result';
    default:
      return 'No sweep yet';
  }
}

/** Bounds for the map's first view: California, which is where the cameras are. */
export const CALIFORNIA_BOUNDS: [[number, number], [number, number]] = [
  [-124.6, 32.4],
  [-114.0, 42.1],
];
