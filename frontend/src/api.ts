/** Thin typed client for the API in api/README.md. Same origin in production;
 *  the Vite dev proxy forwards `/api` to the Rust dev server. */

import type {
  CamerasResponse,
  ClassesResponse,
  Fl511CamerasResponse,
  LaneTotalsResponse,
  LiveStatus,
  MapCamerasResponse,
  TimeBinsResponse,
} from './types';

export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api';
const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === '1';

export class ApiError extends Error {
  readonly status: number;
  /** The API's `error_code`, when it sent one ("worker_unreachable", ...). */
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

type QueryValue = string | number;

function buildUrl(path: string, params: Record<string, QueryValue>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) query.set(key, String(value));
  const suffix = query.toString();
  return `${API_BASE}${path}${suffix ? `?${suffix}` : ''}`;
}

async function getJson<T>(
  path: string,
  params: Record<string, QueryValue>,
  signal?: AbortSignal,
  bearer?: string,
): Promise<T> {
  const headers: Record<string, string> = { accept: 'application/json' };
  if (bearer) headers.authorization = `Bearer ${bearer}`;
  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), { signal, headers });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('The API is not reachable. Is the server running?', 0, 'api_unreachable');
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const message =
      typeof body === 'object' && body !== null && typeof (body as { error?: unknown }).error === 'string'
        ? (body as { error: string }).error
        : `Request failed with HTTP ${response.status}.`;
    const code =
      typeof body === 'object' && body !== null && typeof (body as { error_code?: unknown }).error_code === 'string'
        ? (body as { error_code: string }).error_code
        : null;
    throw new ApiError(message, response.status, code);
  }
  if (body === null || typeof body !== 'object') {
    throw new ApiError('The API returned a response that is not JSON.', response.status);
  }
  return body as T;
}

export async function fetchCameras(signal?: AbortSignal): Promise<CamerasResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockCameras();
  }
  return getJson<CamerasResponse>('/cameras', {}, signal);
}

export async function fetchLaneTotals(
  cameraId: string,
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<LaneTotalsResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockLaneTotals(cameraId, start, end);
  }
  return getJson<LaneTotalsResponse>('/lane_totals', { camera_id: cameraId, start, end }, signal);
}

export async function fetchTimeBins(
  cameraId: string,
  start: string,
  end: string,
  binSeconds: number,
  signal?: AbortSignal,
): Promise<TimeBinsResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockTimeBins(cameraId, start, end, binSeconds);
  }
  return getJson<TimeBinsResponse>(
    '/time_bins',
    { camera_id: cameraId, start, end, bin_seconds: binSeconds },
    signal,
  );
}

/** The Florida camera list: public since 2026-10-29, no token. */
/** Florida is public (Michael, 2026-10-29): no token, no Authorization header. */
export async function fetchFl511Cameras(signal?: AbortSignal): Promise<Fl511CamerasResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockFl511Cameras();
  }
  return getJson<Fl511CamerasResponse>('/fl511/cameras', {}, signal);
}

/** The map: one GeoJSON feature per camera, level and sweep included. */
export async function fetchMapCameras(signal?: AbortSignal): Promise<MapCamerasResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockMapCameras();
  }
  return getJson<MapCamerasResponse>('/map/cameras', {}, signal);
}

export type LiveStartResult = { status: number; body: unknown };

/**
 * Start a live session. The status code is part of the answer: 202 started, 409 somebody
 * else is running one, 503 the worker is offline, 401 the site's access code is missing or
 * wrong. Those are not exceptions the panel can treat alike, so they are returned rather
 * than thrown.
 */
export async function startLiveSession(
  cameraId: string,
  signal?: AbortSignal,
  accessCode?: string | null,
): Promise<LiveStartResult> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockStartLive(cameraId);
  }
  const headers: Record<string, string> = { 'content-type': 'application/json', accept: 'application/json' };
  if (accessCode) headers.authorization = `Bearer ${accessCode}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/live`, {
      method: 'POST',
      signal,
      headers,
      body: JSON.stringify({ camera_id: cameraId }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    return {
      status: 503,
      body: {
        error: "This browser couldn't reach the site's API. Check your connection.",
        error_code: 'api_unreachable',
      },
    };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

export async function fetchLiveStatus(sessionId: string, signal?: AbortSignal): Promise<LiveStatus> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockLiveStatus(sessionId);
  }
  return getJson<LiveStatus>(`/live/${encodeURIComponent(sessionId)}`, {}, signal);
}

export async function stopLiveSession(sessionId: string, signal?: AbortSignal): Promise<number> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockStopLive();
  }
  try {
    const response = await fetch(`${API_BASE}/live/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
      signal,
    });
    return response.status;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    return 0;
  }
}

export async function fetchClasses(
  cameraId: string,
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<ClassesResponse> {
  if (USE_MOCKS) {
    const mocks = await import('./mocks');
    return mocks.mockClasses(cameraId, start, end);
  }
  return getJson<ClassesResponse>('/classes', { camera_id: cameraId, start, end }, signal);
}
