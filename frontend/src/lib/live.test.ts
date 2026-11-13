import { describe, expect, it } from 'vitest';
import {
  IDLE,
  canRetry,
  failureDetail,
  lostContactView,
  frameUrl,
  isPolling,
  metricLines,
  liveMessages,
  sourceOf,
  startView,
  statusView,
  type LiveView,
  detectorLine,
} from './live';

describe('Florida cameras', () => {
  it('are told apart by their id', () => {
    expect(sourceOf('fl511-2849')).toBe('fl511');
    expect(sourceOf('caltrans-D3-41')).toBe('caltrans');
  });

  it('name FL511 and the still, not Caltrans and a sweep', () => {
    const messages = liveMessages('fl511');
    expect(messages.streamOffline).toBe('Video unavailable from FL511 right now. The latest FL511 still is below.');
    expect(messages.offline).not.toContain('sweep');
    expect(startView(503, { error_code: 'stream_offline' }, 'fl511').message).toContain('FL511');
  });

  it('show the worker’s reason when FL511 has no video, with nothing to retry', () => {
    const view = startView(503, { error: 'FL511 has no live video for this camera; only its still.', error_code: 'no_video' }, 'fl511');
    expect(view.phase).toBe('unavailable');
    expect(view.message).toBe('FL511 has no live video for this camera; only its still.');
    expect(canRetry(view)).toBe(false);
  });
});
import type { LiveStatus } from '../types';

function status(overrides: Partial<LiveStatus> = {}): LiveStatus {
  return {
    state: 'running',
    camera_id: 'caltrans-D3-41',
    elapsed_s: 12,
    level: 'moderate',
    flow_per_min: 31.5,
    density: 7.2,
    speed: 0.8,
    detector: 'yolo',
    frame_seq: 180,
    error: null,
    ...overrides,
  };
}

describe('every failure says what happened', () => {
  const cases: Array<[string, number, Record<string, string>, string, string]> = [
    // [what, status, body, phase, the reason shown]
    ['the GPU PC is off', 503, { error: 'The live worker can’t be reached. The GPU PC may be off.', error_code: 'worker_unreachable' }, 'offline', 'GPU PC'],
    ['the worker is slow', 503, { error: 'The live worker took too long to answer.', error_code: 'worker_timeout' }, 'offline', 'too long'],
    ['the worker crashed', 503, { error: 'The live worker failed: model failed to load', error_code: 'worker_error' }, 'offline', 'model failed to load'],
    ['the browser lost the site', 503, { error: "This browser couldn't reach the site's API.", error_code: 'api_unreachable' }, 'offline', 'browser'],
    ['the stream server is slow', 503, { error: 'The stream server could not be reached. Try again in a minute.', error_code: 'stream_unreachable' }, 'stream_offline', 'stream server'],
    ['Caltrans dropped the camera', 503, { error: "Caltrans isn't serving video for this camera right now.", error_code: 'stream_offline' }, 'stream_offline', 'Caltrans'],
    ['FL511 refused', 503, { error: 'FL511 refused the video: blocked', error_code: 'unavailable' }, 'failed', 'FL511 refused'],
  ];

  for (const [what, code, body, phase, shown] of cases) {
    it(`when ${what}`, () => {
      const view = startView(code, body);
      expect(view.phase).toBe(phase);
      expect(view.code).toBe(body.error_code);
      expect(failureDetail(view)).toContain(shown);
      expect(failureDetail(view)).toContain(`code ${body.error_code}`);
    });
  }

  it('says why a finished session ended', () => {
    const view = statusView(
      { ...IDLE, phase: 'running', sessionId: 's1' },
      status({ state: 'done', elapsed_s: 120, stopped_because: 'reached the two-minute limit' }),
      null,
    );
    expect(view.message).toBe('Live analysis finished after 120 s: reached the two-minute limit.');
    expect(failureDetail(view)).toBeNull();
  });

  it('says when the status checks stop getting through, and when', () => {
    const running: LiveView = { ...IDLE, phase: 'running', sessionId: 's1', status: status({ elapsed_s: 41 }) };
    const view = lostContactView(running, 'The live worker can’t be reached.', 'worker_unreachable');
    expect(view.phase).toBe('failed');
    expect(view.message).toBe('Lost contact with live analysis.');
    expect(failureDetail(view)).toBe('Why: The live worker can’t be reached. (after 41 s) · code worker_unreachable');
    expect(canRetry(view)).toBe(true);
  });
});

describe('the live access code', () => {
  it('asks for it when none was sent, and says a sent one was wrong', () => {
    const asked = startView(401, { error: 'a valid research access token is required' });
    expect(asked.phase).toBe('locked');
    expect(asked.message).toBe('Live analysis needs an access code.');
    expect(startView(401, {}, 'fl511', true).message).toBe('That access code did not work.');
  });

  it('is not something to retry or poll: the code form is the way forward', () => {
    const locked = startView(401, {}, 'caltrans', true);
    expect(canRetry(locked)).toBe(false);
    expect(isPolling(locked)).toBe(false);
    expect(locked.sessionId).toBeNull();
  });
});

describe('a camera whose stream is dead', () => {
  it('reads the worker code rather than guessing from the status', () => {
    const dead = startView(503, { error: "Caltrans isn't serving video for this camera right now.", error_code: 'stream_offline' });
    expect(dead.phase).toBe('stream_offline');
    expect(dead.message).toContain('Video unavailable from Caltrans');
    // A dead stream is not the API being down, and the two must not look the same.
    expect(startView(503, { error: 'live analysis is offline' }).phase).toBe('offline');
    expect(startView(503, { error: 'live analysis is offline' }).message).toContain('offline');
  });

  it('offers a retry for a dead stream and for a failure, not for a busy worker', () => {
    expect(canRetry(startView(503, { error_code: 'stream_offline' }))).toBe(true);
    expect(canRetry(startView(500, { error: 'Something went wrong.' }))).toBe(true);
    expect(canRetry(startView(409, { error: 'another live analysis is running', retry_after_s: 12 }))).toBe(false);
    expect(canRetry(startView(503, { error: 'live analysis is offline' }))).toBe(false);
  });

  it('a session that fails with the offline code shows the same line, not the server text', () => {
    const view = statusView(
      { ...IDLE, phase: 'running', sessionId: 's1' },
      {
        state: 'failed',
        camera_id: 'D3-347',
        elapsed_s: 2,
        level: null,
        flow_per_min: null,
        density: null,
        speed: null,
        detector: null,
        frame_seq: 3,
        error: "Caltrans isn't serving video for this camera right now.",
        error_code: 'stream_offline',
      },
      null,
    );
    expect(view.phase).toBe('stream_offline');
    expect(view.message).toContain('Video unavailable from Caltrans');
  });
});

describe('startView', () => {
  it('remembers the session id from a 202', () => {
    const view = startView(202, { session_id: 'abc', state: 'starting' });
    expect(view.phase).toBe('starting');
    expect(view.sessionId).toBe('abc');
  });

  it('turns a 409 into "busy" with the queue time, and keeps no session', () => {
    const view = startView(409, { error: 'another live analysis is running', retry_after_s: 42 });
    expect(view.phase).toBe('busy');
    expect(view.retryAfterS).toBe(42);
    expect(view.sessionId).toBeNull();
    expect(view.message).toMatch(/another live analysis/i);
  });

  it('turns a 503 into "offline"', () => {
    const view = startView(503, { error: 'live analysis is offline' });
    expect(view.phase).toBe('offline');
    expect(view.message).toMatch(/offline/i);
  });

  it('treats an unexpected status as a failure with the API message', () => {
    const view = startView(500, { error: 'boom' });
    expect(view.phase).toBe('failed');
    expect(view.message).toBe('boom');
  });

  it('does not pretend a 202 without a session id started anything', () => {
    expect(startView(202, {}).phase).toBe('failed');
  });

  it('never asks for frames or polling before a session exists', () => {
    expect(frameUrl(null, 3)).toBeNull();
    expect(isPolling(IDLE)).toBe(false);
    expect(isPolling(startView(409, {}))).toBe(false);
  });
});

describe('polling a session', () => {
  it('follows starting and running', () => {
    let view = startView(202, { session_id: 'abc', state: 'starting' });
    view = statusView(view, status({ state: 'starting' }), null);
    expect(view.phase).toBe('starting');
    view = statusView(view, status(), null);
    expect(view.phase).toBe('running');
    expect(isPolling(view)).toBe(true);
  });

  it('stops polling when the session is done', () => {
    let view = startView(202, { session_id: 'abc', state: 'starting' });
    view = statusView(view, status({ state: 'done' }), null);
    expect(view.phase).toBe('done');
    expect(isPolling(view)).toBe(false);
  });

  it('a failed session carries the worker own message', () => {
    let view = startView(202, { session_id: 'abc', state: 'starting' });
    view = statusView(view, status({ state: 'failed', error: 'camera view changed: re-learn the lanes' }), null);
    expect(view.phase).toBe('failed');
    expect(view.message).toBe('Live analysis stopped.');
    expect(view.reason).toBe('camera view changed: re-learn the lanes (after 12 s)');
  });

  it('a poll that throws becomes a failure, not a stale "running"', () => {
    const view = statusView(startView(202, { session_id: 'abc' }), null, 'The API is not reachable.');
    expect(view.phase).toBe('failed');
  });

  it('frames break the cache with the sequence number', () => {
    expect(frameUrl('abc', 12)).toBe('/api/live/abc/frame.jpg?seq=12');
  });
});

describe('the numbers under the verdict', () => {
  it('shows flow, density, speed and the detector', () => {
    const view: LiveView = { ...IDLE, phase: 'running', sessionId: 'abc', status: status() };
    expect(metricLines(view)).toEqual([
      '32 vehicles/min',
      '7.2 in the road',
      '0.80 box-heights/s',
      'yolo detector',
    ]);
  });

  it('says why the detector was chosen when the worker does', () => {
    const at = (phase: 'day' | 'twilight' | 'night' | null, mode: 'yolo' | 'headlights', brightness = 52.4) =>
      detectorLine(status({ light_source: { phase, mode, brightness } }));
    expect(at('day', 'yolo')).toBe('YOLO (daylight)');
    expect(at('night', 'headlights')).toBe('Headlights (night)');
    expect(at('twilight', 'headlights')).toBe('Headlights (twilight, brightness 52)');
    expect(at(null, 'yolo', 120)).toBe('YOLO (location unknown, brightness 120)');
    expect(detectorLine(status({ detector: null }))).toBeNull();
  });

  it('shows nothing until the first window has been measured', () => {
    const view: LiveView = { ...IDLE, phase: 'running', status: status({ flow_per_min: null }) };
    expect(metricLines(view)).toEqual([]);
  });
});
