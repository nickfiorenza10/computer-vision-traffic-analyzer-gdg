/** Types mirroring the endpoint contract in api/README.md. Snake_case on the wire. *
 *  Times are RFC 3339 in UTC ending in `Z`. */

export type Camera = {
  camera_id: string;
  sources: number;
  events: number;
  first_event: string | null;
  last_event: string | null;
};

export type LaneTotal = {
  lane: number;
  direction: string;
  vehicle_count: number;
};

export type TimeBin = {
  bin_start: string;
  lane: number;
  direction: string;
  vehicle_count: number;
};

export type ClassCount = {
  lane: number;
  direction: string;
  vehicle_class: string;
  vehicle_count: number;
};

export type CamerasResponse = { cameras: Camera[] };

export type LaneTotalsResponse = {
  camera_id: string;
  start: string;
  end: string;
  totals: LaneTotal[];
};

export type TimeBinsResponse = {
  camera_id: string;
  start: string;
  end: string;
  bin_seconds: number;
  bins: TimeBin[];
};

export type ClassesResponse = {
  camera_id: string;
  start: string;
  end: string;
  classes: ClassCount[];
};

/** The live traffic map: `GET /api/map/cameras` is GeoJSON, one feature per camera. */
/** `unknown`: the sun was up but the road was dark, a camera problem rather than a reading. */
export type MapLevel = 'heavy' | 'moderate' | 'light' | 'learning' | 'night' | 'unknown';

export type MapCameraProperties = {
  camera_id: string;
  name: string;
  county: string;
  route: string;
  direction: string;
  has_stream: boolean;
  image_url: string | null;
  swept: boolean;
  level: MapLevel | null;
  vehicles_mean: number | null;
  swept_at: string | null;
};

export type MapCameraFeature = {
  type: 'Feature';
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: MapCameraProperties;
};

export type MapCamerasResponse = {
  type: 'FeatureCollection';
  features: MapCameraFeature[];
};

export type LiveState = 'starting' | 'running' | 'done' | 'failed';

export type LightSource = {
  /** The sun at the camera; null when the worker doesn't know where the camera is. */
  phase: 'day' | 'twilight' | 'night' | null;
  brightness: number;
  mode: 'yolo' | 'headlights';
};

export type LiveStartResponse = { session_id: string; state: LiveState };

export type LiveStatus = {
  state: LiveState;
  camera_id: string;
  elapsed_s: number;
  level: MapLevel | null;
  flow_per_min: number | null;
  density: number | null;
  speed: number | null;
  detector: string | null;
  /** Why this detector: the sun at the camera, the road's brightness, and the mode chosen. */
  light_source?: LightSource | null;
  frame_seq: number;
  error: string | null;
  /** Why a session failed, when the worker says why: e.g. "stream_offline". */
  error_code?: string | null;
  /** Why a finished session ended: "reached the two-minute limit", "nobody polled for 30 s". */
  stopped_because?: string | null;
};

/** The Florida camera list, mirroring `GET /api/fl511/cameras` (public since 2026-10-29). */
export type Fl511Camera = {
  camera_id: string;
  description: string;
  road: string;
  direction: string;
  latitude: number;
  longitude: number;
  image_url: string;
};

export type Fl511CamerasResponse = {
  source: string;
  /** The end date FDOT granted, or null when there is none (confirmed 2026-10-29). */
  authorized_through: string | null;
  bbox: [number, number, number, number];
  cameras: Fl511Camera[];
};

/** The five classes the pipeline can emit (pipeline/track_count.py CLASS_NAMES). */
export const VEHICLE_CLASSES = ['bicycle', 'car', 'motorcycle', 'bus', 'truck'] as const;
