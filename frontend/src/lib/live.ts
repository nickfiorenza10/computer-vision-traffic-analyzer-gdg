/** The live-analysis panel's state machine, as pure functions.
 *
 *  The panel can be told four different things when it asks for a live session — started,
 *  busy, offline, or a camera with no stream at all — and each has to look different. Keeping
 *  that mapping here means the component only renders, and the rules can be tested.
 */

import type { LiveStatus } from '../types';

export type LivePhase =
  | 'idle' // the panel is open but nothing has been asked for yet
  | 'unavailable' // this camera has no stream: the sweep result is all there is
  | 'starting'
  | 'running'
  | 'done'
  | 'failed'
  | 'busy' // somebody else's session is running
  | 'offline' // the worker behind the API is unreachable
  | 'stream_offline' // the worker is fine, Caltrans or FL511 is not serving this camera
  | 'locked'; // the site has an access code for live analysis and we lack the right one

export type LiveView = {
  phase: LivePhase;
  sessionId: string | null;
  status: LiveStatus | null;
  retryAfterS: number | null;
  message: string | null;
  /** What actually went wrong, in the worker's or the API's own words, when they said. */
  reason: string | null;
  /** The code that came with it ("stream_unreachable", "worker_timeout", ...). */
  code: string | null;
  /** The frame to show: bumped to break the browser cache on every poll. */
  frameSeq: number;
};

export const IDLE: LiveView = {
  phase: 'idle',
  sessionId: null,
  status: null,
  retryAfterS: null,
  message: null,
  reason: null,
  code: null,
  frameSeq: 0,
};

/** Where a camera's video comes from: its id says ("fl511-2849" is Florida). */
export type LiveSource = 'caltrans' | 'fl511';

export function sourceOf(cameraId: string): LiveSource {
  return cameraId.startsWith('fl511-') ? 'fl511' : 'caltrans';
}

/** The panel's sentences, naming the right provider and what is shown instead of live video. */
export function liveMessages(source: LiveSource = 'caltrans') {
  const provider = source === 'fl511' ? 'FL511' : 'Caltrans';
  const fallback = source === 'fl511' ? 'The latest FL511 still' : 'The latest sweep';
  return {
    offline: `Live analysis is offline. ${fallback} is below.`,
    streamOffline: `Video unavailable from ${provider} right now. ${fallback} is below.`,
    busy: 'Another live analysis is running.',
    unavailable: `This camera has no live stream. ${fallback} is below.`,
    failed: 'Live analysis stopped.',
    refused: 'This camera could not be analysed.',
    lostContact: 'Lost contact with live analysis.',
    locked: 'Live analysis needs an access code.',
    wrongCode: 'That access code did not work.',
    /** For "... is above." once the live block sits under the still. */
    fallback,
  };
}

export const LIVE_MESSAGES = liveMessages('caltrans');

/**
 * The API answers 202, 409 or 503 when a session starts, and 401 when the site has an access
 * code (`sentCode` says whether we sent one, so a wrong code reads differently from none);
 * anything else is a real error.
 */
export function startView(
  status: number,
  body: unknown,
  source: LiveSource = 'caltrans',
  sentCode = false,
): LiveView {
  const messages = liveMessages(source);
  if (status === 401) {
    return { ...IDLE, phase: 'locked', message: sentCode ? messages.wrongCode : messages.locked };
  }
  if (status === 202 && isRecord(body) && typeof body.session_id === 'string') {
    return {
      ...IDLE,
      phase: 'starting',
      sessionId: body.session_id,
      status: {
        state: 'starting',
        camera_id: '',
        elapsed_s: 0,
        level: null,
        flow_per_min: null,
        density: null,
        speed: null,
        detector: null,
        frame_seq: 0,
        error: null,
      },
    };
  }
  if (status === 409) {
    return {
      ...IDLE,
      phase: 'busy',
      retryAfterS: isRecord(body) && typeof body.retry_after_s === 'number' ? body.retry_after_s : null,
      message: messages.busy,
    };
  }
  const reason = isRecord(body) && typeof body.error === 'string' ? body.error : null;
  const code = isRecord(body) && typeof body.error_code === 'string' ? body.error_code : null;
  if (status === 503) {
    // Every 503 carries a code naming the part that failed; the line on top is ours, the
    // reason under it is theirs, so nobody is left looking at a bare "offline".
    if (code === 'no_video') {
      // FL511 lists some cameras with a still but no video: nothing to retry.
      return { ...IDLE, phase: 'unavailable', message: reason ?? messages.unavailable, code };
    }
    if (code && STREAM_CODES.has(code)) {
      return { ...IDLE, phase: 'stream_offline', message: messages.streamOffline, reason, code };
    }
    if (code && !WORKER_CODES.has(code)) {
      // The worker looked at this camera and said no for its own reason.
      return { ...IDLE, phase: 'failed', message: messages.refused, reason, code };
    }
    return { ...IDLE, phase: 'offline', message: messages.offline, reason, code };
  }
  return { ...IDLE, phase: 'failed', message: reason ?? messages.failed, code };
}

/** The camera's stream is the problem: worth trying again in a minute. */
const STREAM_CODES = new Set(['stream_offline', 'stream_unreachable']);
/** The API could not get an answer from the worker at all. */
const WORKER_CODES = new Set([
  'worker_unreachable',
  'worker_timeout',
  'worker_not_configured',
  'worker_bad_reply',
  'worker_error',
  'api_unreachable',
]);

/** Fold a status poll into the view. */
export function statusView(
  view: LiveView,
  status: LiveStatus | null,
  error: string | null,
  source: LiveSource = 'caltrans',
): LiveView {
  const messages = liveMessages(source);
  if (error || !status) {
    return { ...view, phase: 'failed', message: error ?? messages.failed };
  }
  const code = status.error_code ?? null;
  const phase: LivePhase =
    status.state === 'starting' || status.state === 'running'
      ? status.state
      : status.state === 'done'
        ? 'done'
        : code && STREAM_CODES.has(code)
          ? 'stream_offline'
          : 'failed';
  const at = status.elapsed_s ? ` after ${Math.round(status.elapsed_s)} s` : '';
  const broke = phase === 'stream_offline' || phase === 'failed';
  const said = status.error ? (at ? `${status.error} (${at.trim()})` : status.error) : null;
  return {
    ...view,
    phase,
    status,
    frameSeq: status.frame_seq,
    message:
      phase === 'stream_offline'
        ? messages.streamOffline
        : phase === 'failed'
          ? messages.failed
          : phase === 'done' && status.stopped_because
            ? `Live analysis finished${at}: ${status.stopped_because}.`
            : null,
    reason: broke ? said : null,
    code: broke ? code : null,
  };
}

/**
 * The status poll itself failing mid-session: after a few misses in a row the panel says so,
 * instead of freezing on the last frame. `error` is the API's own message.
 */
export function lostContactView(view: LiveView, error: string, code: string | null, source: LiveSource = 'caltrans'): LiveView {
  const at = view.status?.elapsed_s ? ` after ${Math.round(view.status.elapsed_s)} s` : '';
  return {
    ...view,
    phase: 'failed',
    message: liveMessages(source).lostContact,
    reason: `${error}${at ? ` (${at.trim()})` : ''}`,
    code,
  };
}

/** "Why: ... · code stream_unreachable", for the line under a failure. */
export function failureDetail(view: LiveView): string | null {
  if (!view.reason && !view.code) return null;
  return [view.reason ? `Why: ${view.reason}` : null, view.code ? `code ${view.code}` : null]
    .filter(Boolean)
    .join(' · ');
}

/** Only a dead stream is worth asking about again straight away. */
export function canRetry(view: LiveView): boolean {
  return view.phase === 'stream_offline' || view.phase === 'failed';
}

/** A session that is over, or one we never started, needs no timer. */
export function isPolling(view: LiveView): boolean {
  return view.phase === 'starting' || view.phase === 'running';
}

export function frameUrl(sessionId: string | null, seq: number): string | null {
  if (!sessionId) return null;
  return `/api/live/${encodeURIComponent(sessionId)}/frame.jpg?seq=${seq}`;
}

/** The panel's big word for a live session: nothing measured yet reads "Measuring…". */
export function liveHeadline(level: string | null): string {
  if (!level) return 'Measuring…';
  return level === 'heavy'
    ? 'Heavy traffic'
    : level === 'moderate'
      ? 'Moderate traffic'
      : level === 'light'
        ? 'Light traffic'
        : level;
}

/** A stand-in frame for the offline mocks, so the panel is not a black box without the API. */
export const MOCK_FRAME =
  'data:image/' +
  'svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">' +
      '<rect width="640" height="480" fill="#10161b"/>' +
      '<text x="320" y="230" fill="#7fe7c4" font-family="sans-serif" font-size="24" text-anchor="middle">annotated live frame (mock)</text>' +
      '<text x="320" y="264" fill="#9fb3ad" font-family="sans-serif" font-size="15" text-anchor="middle">the worker sends a JPEG here</text>' +
      '</svg>',
  );

/** "Heavy traffic", with the numbers that produced it underneath. */
export function metricLines(view: LiveView): string[] {
  const status = view.status;
  if (!status || status.flow_per_min === null) return [];
  const lines = [
    `${Math.round(status.flow_per_min)} vehicles/min`,
    `${status.density?.toFixed(1) ?? '—'} in the road`,
    `${status.speed?.toFixed(2) ?? '—'} box-heights/s`,
  ];
  const detector = detectorLine(status);
  if (detector) lines.push(detector);
  return lines;
}

/** "YOLO (daylight)", "Headlights (night)", "Headlights (twilight, brightness 52)". */
export function detectorLine(status: LiveStatus): string | null {
  const source = status.light_source;
  if (!source) return status.detector ? `${status.detector} detector` : null;
  const mode = source.mode === 'headlights' ? 'Headlights' : 'YOLO';
  if (source.phase === 'day') return `${mode} (daylight)`;
  if (source.phase === 'night') return `${mode} (night)`;
  const why = source.phase === 'twilight' ? 'twilight' : 'location unknown';
  return `${mode} (${why}, brightness ${Math.round(source.brightness)})`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
