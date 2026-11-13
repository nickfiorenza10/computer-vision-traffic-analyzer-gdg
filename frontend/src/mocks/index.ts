/** Offline fixtures for working on the UI without a database or the Rust API.
 *
 *  Captured from the real endpoints (api/README.md contract) running against the
 *  sample data imported in T2, so the shapes are exactly what the API returns.
 *  Enable with `VITE_USE_MOCKS=1 npm run dev`.
 */

import camerasJson from './data/cameras.json';
import classesJson from './data/classes.json';
import laneTotalsJson from './data/lane_totals.json';
import mapCamerasJson from './data/map_cameras.json';
import timeBinsJson from './data/time_bins.json';
import type {
  CamerasResponse,
  ClassesResponse,
  Fl511CamerasResponse,
  LaneTotalsResponse,
  LiveStatus,
  MapCamerasResponse,
  TimeBinsResponse,
} from '../types';

/** The map's GeoJSON: every level represented, plus cameras with no sweep at all.
 *  Still URLs point at a drawn placeholder, so the panel looks like it does in production
 *  without reaching Caltrans from a development machine. */
const STILL_PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">' +
      '<rect width="640" height="480" fill="#1b2126"/>' +
      '<rect y="300" width="640" height="180" fill="#2b333a"/>' +
      '<rect x="120" y="250" width="90" height="60" fill="#c9d1d9"/>' +
      '<rect x="330" y="240" width="80" height="70" fill="#98a4ae"/>' +
      '<text x="24" y="40" fill="#e6edf3" font-family="monospace" font-size="20">I-80 · Sacramento (mock still)</text>' +
      '</svg>',
  );

export function mockMapCameras(): MapCamerasResponse {
  const data = mapCamerasJson as unknown as MapCamerasResponse;
  return {
    ...data,
    features: data.features.map((feature) => ({
      ...feature,
      properties: {
        ...feature.properties,
        image_url: feature.properties.swept ? STILL_PLACEHOLDER : null,
      },
    })),
  };
}

/** A live session that starts, runs for a few polls and finishes, so the panel can be used offline. */
const liveStartedAt = Date.now();
const liveSessions = new Map<string, number>();

// One camera stands in for the dead-stream case so the panel can be exercised offline.
const DEAD_STREAMS = new Set<string>(['caltrans-D3-8']);

export function mockStartLive(cameraId: string): { status: number; body: unknown } {
  if (DEAD_STREAMS.has(cameraId)) {
    return {
      status: 503,
      body: {
        error: "Caltrans isn't serving video for this camera right now.",
        error_code: 'stream_offline',
      },
    };
  }
  const sessionId = `mock-${cameraId}-${liveSessions.size + 1}`;
  liveSessions.set(sessionId, Date.now());
  return { status: 202, body: { session_id: sessionId, state: 'starting' } };
}

export function mockLiveStatus(sessionId: string): LiveStatus {
  const started = liveSessions.get(sessionId) ?? liveStartedAt;
  const elapsed = (Date.now() - started) / 1000;
  const state = elapsed < 2 ? 'starting' : elapsed < 40 ? 'running' : 'done';
  const level = elapsed < 6 ? null : 'moderate';
  return {
    state,
    camera_id: sessionId.split('-')[1] ?? 'D3-1',
    elapsed_s: Math.round(elapsed),
    level,
    flow_per_min: level ? 34 + Math.round(elapsed % 7) : null,
    density: level ? 4.2 : null,
    speed: level ? 0.71 : null,
    detector: 'yolo',
    frame_seq: Math.round(elapsed * 15),
    error: null,
  };
}

export function mockStopLive(): number {
  return 200;
}

export function mockCameras(): CamerasResponse {
  return camerasJson as CamerasResponse;
}

export function mockLaneTotals(cameraId: string, start: string, end: string): LaneTotalsResponse {
  return { ...(laneTotalsJson as LaneTotalsResponse), camera_id: cameraId, start, end };
}

export function mockTimeBins(
  cameraId: string,
  start: string,
  end: string,
  binSeconds: number,
): TimeBinsResponse {
  return { ...(timeBinsJson as TimeBinsResponse), camera_id: cameraId, start, end, bin_seconds: binSeconds };
}

export function mockClasses(cameraId: string, start: string, end: string): ClassesResponse {
  return { ...(classesJson as ClassesResponse), camera_id: cameraId, start, end };
}

/** Florida fixture: three Tampa cameras, with an inline placeholder image so
 *  the UI can be exercised offline. No FL511 host is ever contacted from a mock. */
const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="480"><rect width="720" height="480" fill="#1a2228"/>' +
      '<text x="360" y="230" fill="#7fe7c4" font-family="sans-serif" font-size="26" text-anchor="middle">FL511 snapshot placeholder</text>' +
      '<text x="360" y="266" fill="#9fb3ad" font-family="sans-serif" font-size="16" text-anchor="middle">mock fixture, not a real camera</text></svg>',
  );

export function mockFl511Cameras(): Fl511CamerasResponse {
  return {
    source: 'FL511',
    authorized_through: null,
    bbox: [-82.6, 27.8, -82.3, 28.1],
    cameras: [
      {
        camera_id: '1267',
        description: 'Gandy at Dale Mabry Hwy',
        road: 'SR 600/Gandy Blvd',
        direction: 'E',
        latitude: 27.893438,
        longitude: -82.506656,
        image_url: PLACEHOLDER,
      },
      {
        camera_id: '838',
        description: 'Himes at Lemon St',
        road: 'Himes Avenue',
        direction: 'S',
        latitude: 27.951199,
        longitude: -82.501374,
        image_url: PLACEHOLDER,
      },
      {
        camera_id: '757',
        description: 'at 6th Ave',
        road: 'CTC',
        direction: 'N',
        latitude: 27.958789,
        longitude: -82.425631,
        image_url: PLACEHOLDER,
      },
    ],
  };
}
