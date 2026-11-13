import { useState } from 'react';

import { fetchFl511Cameras } from '../api';
import type { Fl511Camera } from '../types';

const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === '1';

/**
 * The short static page the mockup's third nav link points at: what the sweep does, what live
 * analysis does, what the levels mean, day and night, and where the data comes from.
 */
export function HowItWorks() {
  return (
    <main className="page prose">
      <h1>How it works</h1>

      <section>
        <h2>The sweep</h2>
        <p>
          Every 30 minutes the worker photographs each camera eight times and counts the vehicles it can see,
          using YOLO in daylight and a headlight detector after dark. Each sweep leaves one row per camera with
          the mean vehicles in frame and a traffic level. A camera needs eight sweeps before it reports a level;
          until then it says <em>learning</em>.
        </p>
      </section>

      <section>
        <h2>Live analysis</h2>
        <p>
          Click a camera and the worker opens its stream, draws the lane lines it was taught, and counts vehicles
          per lane for as long as you watch, up to two minutes. One live analysis runs at a time, because the
          counting needs the whole machine. The frame you see is the last one it measured, never a guess.
        </p>
      </section>

      <section>
        <h2>The levels, in plain words</h2>
        <ul>
          <li><strong>Heavy</strong>: traffic is at a crawl and vehicles are packed close together.</li>
          <li><strong>Moderate</strong>: moving, but you would not call it free-flowing.</li>
          <li><strong>Light</strong>: room to change lanes and to keep your distance.</li>
          <li><strong>Learning</strong>: not enough sweeps yet to say anything.</li>
          <li><strong>Night</strong>: the camera is in the dark; headlights count vehicles but cannot show congestion.</li>
        </ul>
        <p>
          A county's level comes from its cameras: heavy when at least a quarter of them are heavy, moderate when
          at least 40% are heavy or moderate, light otherwise. Fewer than eight sweeps, or a sweep older than 45
          minutes, and the county says so instead of guessing.
        </p>
      </section>

      <section>
        <h2>Day and night</h2>
        <p>
          Sunrise and sunset are computed for each camera's own latitude and longitude, so “night” is the sun's
          position, not a guess from how dark the picture looks. A camera that is dark in daylight is reported as
          a camera problem, not as night.
        </p>
      </section>

      <section>
        <h2>Where the data comes from</h2>
        <p>
          Caltrans CWWP2 cameras and FL511 cameras (used with FDOT's permission). County outlines come from the US
          Census; the map is OpenStreetMap. Missing readings mean no data, never zero traffic.
        </p>
      </section>
    </main>
  );
}

/** The Florida camera list without the research gate: public since 2026-10-29. */
export function FloridaCameraList() {
  const [cameras, setCameras] = useState<Fl511Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (cameras === null && error === null) {
    fetchFl511Cameras()
      .then((response) => setCameras(response.cameras))
      .catch(() => setError('The Florida camera list did not load.'));
  }

  return (
    <section className="fl-list">
      <h2>Florida cameras</h2>
      {error ? <p className="note">{error}</p> : null}
      {cameras === null && !error ? <p className="muted">Loading…</p> : null}
      <ul>
        {(cameras ?? []).map((camera) => (
          <li key={camera.camera_id}>
            <span className="route-chip">{camera.road}</span> {camera.description}
            {USE_MOCKS ? <span className="muted"> · mock</span> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
