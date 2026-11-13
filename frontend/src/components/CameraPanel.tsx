import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, fetchLiveStatus, startLiveSession, stopLiveSession } from '../api';
import { forgetAccessCode, readAccessCode, saveAccessCode } from '../lib/accessCode';
import {
  IDLE,
  canRetry,
  failureDetail,
  liveMessages,
  lostContactView,
  sourceOf,
  MOCK_FRAME,
  frameUrl,
  isPolling,
  liveHeadline,
  metricLines,
  startView,
  statusView,
  type LiveView,
} from '../lib/live';
import { headlineFor, sweepAge } from '../lib/map';
import type { MapCameraFeature } from '../types';

type Props = {
  camera: MapCameraFeature;
  onClose: () => void;
};

const POLL_MS = 1000;
const FRAME_MS = 500;
/** Polls that may fail in a row (a blip) before the panel says contact was lost. */
const MISSES_ALLOWED = 3;

/**
 * The right-hand panel: what this camera last measured, and — if it has a stream — a live
 * analysis that starts by itself. Three things can go wrong and each has its own face:
 * another session running (409), the worker offline (503), or no stream at all.
 */
export function CameraPanel({ camera, onClose }: Props) {
  const properties = camera.properties;
  const source = sourceOf(properties.camera_id);
  const messages = liveMessages(source);
  const [live, setLive] = useState<LiveView>(IDLE);
  const [frameTick, setFrameTick] = useState(0);
  const sessionRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Start a session when the panel opens - and again when someone presses "Try again" -
  // and stop whatever is running when the panel closes.
  const begin = useCallback(() => {
    if (!properties.has_stream) {
      setLive({ ...IDLE, phase: 'unavailable', message: liveMessages(source).unavailable });
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLive({ ...IDLE, phase: 'starting' });
    const code = readAccessCode();
    startLiveSession(properties.camera_id, controller.signal, code)
      .then((result) => {
        if (controller.signal.aborted) return;
        const view = startView(result.status, result.body, source, Boolean(code));
        // A refused code is not worth sending with the next click.
        if (view.phase === 'locked' && code) forgetAccessCode();
        sessionRef.current = view.sessionId;
        setLive(view);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setLive({ ...IDLE, phase: 'offline', message: liveMessages(source).offline });
      });
  }, [properties.camera_id, properties.has_stream, source]);

  useEffect(() => {
    begin();
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
      const session = sessionRef.current;
      sessionRef.current = null;
      if (session) void stopLiveSession(session);
    };
  }, [begin]);

  // Poll the session while it lives.
  useEffect(() => {
    const session = live.sessionId;
    if (!session || !isPolling(live)) return;
    const controller = new AbortController();
    let misses = 0;
    const timer = window.setInterval(() => {
      fetchLiveStatus(session, controller.signal)
        .then((status) => {
          misses = 0;
          setLive((current) => statusView(current, status, null, source));
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') return;
          if (error instanceof ApiError && error.status === 404) {
            setLive((current) => ({
              ...current,
              phase: 'failed',
              message: liveMessages(source).lostContact,
              reason: 'The worker no longer has this session. It may have restarted.',
              code: 'session_gone',
            }));
            return;
          }
          misses += 1;
          if (misses < MISSES_ALLOWED) return;
          const message = error instanceof Error ? error.message : 'The status check failed.';
          const code = error instanceof ApiError ? error.code : null;
          setLive((current) => lostContactView(current, message, code, source));
        });
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [live, source]);

  // Frames refresh twice a second, faster than the status poll, while a session runs.
  useEffect(() => {
    if (!isPolling(live)) return;
    const timer = window.setInterval(() => setFrameTick((value) => value + 1), FRAME_MS);
    return () => window.clearInterval(timer);
  }, [live.phase]);

  const [codeDraft, setCodeDraft] = useState('');
  const unlock = (event: FormEvent) => {
    event.preventDefault();
    const code = codeDraft.trim();
    if (!code) return;
    saveAccessCode(code);
    setCodeDraft('');
    begin();
  };

  const stop = () => {
    const session = live.sessionId;
    if (!session) return;
    void stopLiveSession(session);
    sessionRef.current = null;
    setLive((current) => ({ ...current, phase: 'done', message: 'Stopped.' }));
  };

  const status = live.status;
  const level = status?.level ?? properties.level;
  const metrics = metricLines(live);
  const mockFrame = import.meta.env.VITE_USE_MOCKS === '1';
  const frame = mockFrame ? MOCK_FRAME : frameUrl(live.sessionId, frameTick * 1000 + (status?.frame_seq ?? 0));
  const showLive = properties.has_stream && live.phase !== 'unavailable';
  const detail = failureDetail(live);

  return (
    <aside className="camera-panel" aria-label={`Camera ${properties.camera_id}`}>
      <header className="panel-head">
        <div>
          <h2>{properties.name || properties.camera_id}</h2>
          <div className="sub">
            {[properties.route, properties.direction, properties.county].filter(Boolean).join(' · ')}
          </div>
        </div>
        <button type="button" className="link-button" onClick={onClose} aria-label="Close the camera panel">
          Close
        </button>
      </header>

      <section className={`sweep-block level-${properties.level ?? 'none'}`}>
        <div className="verdict">{source === 'fl511' ? 'Latest FL511 still' : headlineFor(properties.level)}</div>
        <div className="sub">
          {source === 'fl511'
            ? 'FL511 refreshes this picture about once a minute. Florida cameras are not swept.'
            : properties.swept
            ? `${sweepAge(properties)} · ${
                properties.vehicles_mean === null ? 'no count' : `${properties.vehicles_mean.toFixed(1)} vehicles per still`
              }`
            : 'No sweep has run for this camera yet.'}
        </div>
        {properties.image_url ? (
          <img className="sweep-still" src={properties.image_url} alt={`Latest sweep still of ${properties.name}`} />
        ) : (
          <p className="note">This camera has no still image.</p>
        )}
      </section>

      {showLive ? (
        <section className={`live-block live-${live.phase}`} aria-live="polite">
          <div className="live-head">
            <strong>Live analysis</strong>
            {isPolling(live) ? <span className="pulse">● live</span> : null}
            {status?.elapsed_s ? <span className="sub">{Math.round(status.elapsed_s)}s</span> : null}
          </div>

          {frame && (live.phase === 'running' || live.phase === 'starting') ? (
            <img className="live-frame" src={frame} alt="Annotated live frame from the worker" />
          ) : null}

          {live.phase === 'busy' ? (
            <p className="note">
              {messages.busy} {live.retryAfterS ? `About ${live.retryAfterS}s left.` : ''} {messages.fallback} is
              above.
            </p>
          ) : null}
          {live.phase === 'offline' ? <p className="note">{live.message ?? messages.offline}</p> : null}
          {live.phase === 'stream_offline' ? <p className="note">{messages.streamOffline}</p> : null}
          {live.phase === 'locked' ? (
            <form className="access-form" onSubmit={unlock}>
              <p className={live.message === messages.wrongCode ? 'state error' : 'note'}>{live.message}</p>
              <div className="field">
                <label htmlFor={`access-${properties.camera_id}`}>Access code</label>
                <input
                  id={`access-${properties.camera_id}`}
                  type="password"
                  autoComplete="off"
                  value={codeDraft}
                  onChange={(event) => setCodeDraft(event.target.value)}
                />
              </div>
              <button type="submit" disabled={!codeDraft.trim()}>
                Start live analysis
              </button>
            </form>
          ) : null}

          {status && (live.phase === 'running' || live.phase === 'done') ? (
            <>
              <div className={`verdict level-${level ?? 'none'}`}>{liveHeadline(level)}</div>
              <ul className="live-metrics">
                {metrics.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          ) : null}

          {live.phase === 'failed' ? <p className="state error">{live.message ?? messages.failed}</p> : null}
          {live.phase === 'done' ? <p className="note">{live.message ?? 'Live analysis finished.'}</p> : null}
          {detail && (live.phase === 'offline' || live.phase === 'stream_offline' || live.phase === 'failed') ? (
            <p className="live-reason">{detail}</p>
          ) : null}

          {isPolling(live) ? (
            <button type="button" onClick={stop}>
              Stop live analysis
            </button>
          ) : null}
          {canRetry(live) ? (
            <button type="button" onClick={begin}>
              Try again
            </button>
          ) : null}
        </section>
      ) : (
        <p className="note">{live.message ?? messages.unavailable}</p>
      )}
    </aside>
  );
}
