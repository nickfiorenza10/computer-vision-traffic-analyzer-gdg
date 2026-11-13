/** Which county a camera is in, from the county shapes the map draws.
 *
 *  The shapes come from `public/geo/counties-*.json`, built by `npm run build:counties` from
 *  the US Census atlas. Nothing here is a projection: a camera's latitude and longitude are
 *  tested against the shapes in degrees, which is exact enough at county scale and needs no
 *  map instance.
 */

export type Ring = [number, number][];
export type CountyGeometry =
  | { type: 'Polygon'; coordinates: Ring[] }
  | { type: 'MultiPolygon'; coordinates: Ring[][] };

export type CountyFeature = {
  type: 'Feature';
  properties: { name: string; fips: string };
  geometry: CountyGeometry;
};

export type CountyCollection = { type: 'FeatureCollection'; features: CountyFeature[] };

/** Ray casting over one ring. Points exactly on the border are "inside" by this test. */
export function pointInRing(longitude: number, latitude: number, ring: Ring): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    const straddles = yi > latitude !== yj > latitude;
    if (!straddles) continue;
    const crossing = xi + ((latitude - yi) * (xj - xi)) / (yj - yi);
    if (longitude < crossing) inside = !inside;
  }
  return inside;
}

/** A polygon is its outer ring minus its holes. */
export function pointInPolygon(longitude: number, latitude: number, rings: Ring[]): boolean {
  if (rings.length === 0) return false;
  if (!pointInRing(longitude, latitude, rings[0])) return false;
  return !rings.slice(1).some((hole) => pointInRing(longitude, latitude, hole));
}

export function pointInCounty(
  longitude: number,
  latitude: number,
  geometry: CountyGeometry,
): boolean {
  if (geometry.type === 'Polygon') {
    return pointInPolygon(longitude, latitude, geometry.coordinates);
  }
  return geometry.coordinates.some((rings) => pointInPolygon(longitude, latitude, rings));
}

/**
 * The county a camera is in: the first match wins, so a camera exactly on a border lands in
 * whichever county comes first in the file (the shapes are sorted by name, so this is stable).
 * Cameras that match nothing are "Unassigned".
 */
export function countyFor(
  longitude: number,
  latitude: number,
  counties: CountyFeature[],
): string | null {
  for (const county of counties) {
    if (pointInCounty(longitude, latitude, county.geometry)) return county.properties.name;
  }
  return null;
}

export const UNASSIGNED = 'Unassigned';

export type Located = { id: string; county: string; longitude: number; latitude: number };

/** Attach a county to every camera, and one line per unassigned camera (never per frame). */
export function locateCameras(
  cameras: { id: string; longitude: number; latitude: number }[],
  counties: CountyFeature[],
  log: (message: string) => void = () => {},
): Located[] {
  const placed: Located[] = [];
  const complained = new Set<string>();
  for (const camera of cameras) {
    const county = countyFor(camera.longitude, camera.latitude, counties);
    if (county === null && !complained.has(camera.id)) {
      complained.add(camera.id);
      log(`camera ${camera.id} at ${camera.latitude.toFixed(4)}, ${camera.longitude.toFixed(4)} is outside every county shape`);
    }
    placed.push({
      id: camera.id,
      county: county ?? UNASSIGNED,
      longitude: camera.longitude,
      latitude: camera.latitude,
    });
  }
  return placed;
}
