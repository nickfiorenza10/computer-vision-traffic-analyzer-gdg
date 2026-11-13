import { useEffect, useState } from 'react';

import { hashFor, type Route } from '../lib/routes';
import { sunLine } from '../lib/sunTimes';
import { highwayGroups, onHighway } from '../lib/highways';
import type { CountyVerdict } from '../lib/countyLevel';
import type { MapLevel } from '../types';

const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === '1';

export const LEVEL_LABELS: Record<MapLevel, string> = {
  heavy: 'Heavy traffic',
  moderate: 'Moderate traffic',
  light: 'Light traffic',
  learning: 'Learning this camera',
  night: 'Night: no daylight reading',
  unknown: 'No reading: camera dark in daylight',
};

export const LEVEL_COLOURS: Record<MapLevel, string> = {
  heavy: '#FF6369',
  moderate: '#FFB224',
  light: '#2F8F63',
  learning: '#6E6AB0',
  night: '#343A46',
  unknown: '#191C21',
};

/** A county speaks for several cameras, so its level box says so. */
export function countyLevelLabel(level: MapLevel | 'no_data', cameras: number): string {
  if (level === 'no_data') return 'No cameras swept';
  if (level === 'learning' && cameras !== 1) return 'Learning these cameras';
  return LEVEL_LABELS[level];
}

/** "Sunset 6:09 PM" (or the next sunrise) at a place, refreshed every minute. */
export function useSunLine(at: { lat: number; lon: number } | null): string | null {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return at ? sunLine(at.lat, at.lon, now) : null;
}

/** The header: mark, nav, worker status, and the state switch that is a real route. */
export function SiteHeader({ route, workerOnline }: { route: Route; workerOnline: boolean }) {
  const mapRoute: Route = route === 'florida' ? 'florida' : 'california';
  return (
    <header className="site-header">
      <nav>
        <a className="brandlink" href={hashFor('california')}>
          <svg width="26" height="26" viewBox="0 0 26 26" fill="none" aria-hidden="true">
            <rect x="1" y="1" width="24" height="24" rx="6" stroke="#F2F3F5" strokeWidth="1.6" />
            <path d="M8 19 L11 7 M18 19 L15 7" stroke="#F2F3F5" strokeWidth="1.6" strokeLinecap="round" />
            <path d="M13 9 v2 M13 14 v2" stroke="#3DDC97" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
          <span className="brand">Traffic Analyzer</span>
        </a>
        <div className="navlinks">
          <a href={hashFor(mapRoute)} aria-current={route === 'california' || route === 'florida' ? 'page' : undefined}>
            Map
          </a>
          <a href={hashFor('history')} aria-current={route === 'history' ? 'page' : undefined}>
            History
          </a>
          <a href={hashFor('how')} aria-current={route === 'how' ? 'page' : undefined}>
            How it works
          </a>
        </div>
        <div className="header-right">
          <span className="worker">
            <span className={`dot ${workerOnline ? 'dot-on' : 'dot-off'}`} aria-hidden="true" />
            {workerOnline ? 'Worker online' : 'Worker offline'}
          </span>
          <div role="group" aria-label="State" className="segmented">
            <a href={hashFor('california')} aria-current={mapRoute === 'california' ? 'page' : undefined}>
              California
            </a>
            <a href={hashFor('florida')} aria-current={mapRoute === 'florida' ? 'page' : undefined}>
              Florida
            </a>
          </div>
        </div>
      </nav>
    </header>
  );
}

/** The hero, word for word from the mockup. */
export function Hero({ state }: { state: 'ca' | 'fl' }) {
  const florida = state === 'fl';
  return (
    <div className="hero">
      <span className="pill">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <path d="M12 22s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z" />
          <circle cx="12" cy="10" r="2.5" />
        </svg>
        {florida ? 'Live across Florida' : 'Live across California'}
      </span>
      <h1>
        {florida ? 'How busy is Tampa Bay right now?' : 'How busy is the road right now?'}
        <span className="caret" aria-hidden="true" />
      </h1>
      <p>
        {florida
          ? 'FL511 cameras around Tampa Bay, used with FDOT’s permission. Pick a county on the map to see its cameras and the latest snapshot.'
          : 'Caltrans cameras, swept every 30 minutes and analyzed live when you click one. Day or night: headlights count too. Pick a county on the map, or search for a road.'}
      </p>
    </div>
  );
}

export function LegendRow({ mode }: { mode: 'level' | 'coverage' }) {
  if (mode === 'coverage') {
    return (
      <div role="list" aria-label="Legend" className="legend">
        {[
          ['1–4 cameras', '#1D3B4D'],
          ['5–20', '#24607F'],
          ['21–50', '#3390BD'],
          ['51+', '#7CCBF0'],
          ['None yet', '#191C21'],
        ].map(([label, colour]) => (
          <span role="listitem" key={label}>
            <span className={`swatch ${colour === '#191C21' ? 'swatch-ring' : ''}`} style={{ background: colour }} aria-hidden="true" />
            {label}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div role="list" aria-label="Legend" className="legend">
      {(['heavy', 'moderate', 'light', 'learning', 'night'] as MapLevel[]).map((level) => (
        <span role="listitem" key={level}>
          <span className="swatch" style={{ background: LEVEL_COLOURS[level] }} aria-hidden="true" />
          {LEVEL_LABELS[level]}
        </span>
      ))}
      <span role="listitem">
        <span className="swatch swatch-ring" style={{ background: '#191C21' }} aria-hidden="true" />
        No cameras swept
      </span>
    </div>
  );
}

export function StatTiles({ cameras, swept }: { cameras: number; swept: number }) {
  return (
    <div className="tiles">
      <div>
        <span className="tile-number">{swept || '—'}</span>
        <span className="tile-label">
          {swept === 1 ? 'camera swept' : 'cameras swept'} of {cameras || '—'}
        </span>
      </div>
      <div>
        <span className="tile-number">30 min</span>
        <span className="tile-label">between sweeps, 8 frames each</span>
      </div>
      <div>
        <span className="tile-number">Day + night</span>
        <span className="tile-label">YOLO in daylight, headlights after dark</span>
      </div>
    </div>
  );
}

export type Region = {
  name: string;
  tag: string;
  description: string;
  county?: string;
  href?: string;
  note?: string;
};

export function Regions({ regions, onPick }: { regions: Region[]; onPick: (county: string) => void }) {
  return (
    <>
      <div className="regions-head">
        <span className="kicker">Browse by region</span>
        <h2>Find a camera by region</h2>
      </div>
      <div className="region-grid">
        {regions.map((region) => (
          <div className="card" key={region.name}>
            <div className="card-head">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#C9CDD3" strokeWidth="2" aria-hidden="true">
                <path d="M12 22s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z" />
                <circle cx="12" cy="10" r="2.5" />
              </svg>
              <span className="card-name">{region.name}</span>
              <span className="card-tag">{region.tag}</span>
            </div>
            <p>{region.description}</p>
            {region.county ? (
              <button type="button" className="link" onClick={() => onPick(region.county as string)}>
                See cameras →
              </button>
            ) : null}
            {region.href ? <a className="link" href={region.href}>{region.note ?? 'Switch →'}</a> : null}
            {!region.county && !region.href ? <span className="muted">Not swept yet</span> : null}
          </div>
        ))}
      </div>
    </>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div>
        <span>A GDG project. Missing readings mean no data, never zero traffic.</span>
        <span>Camera data: Caltrans CWWP2, FL511 (with FDOT’s permission) · Map: US Census, OpenStreetMap</span>
      </div>
    </footer>
  );
}

/** The county panel: the level, its reason, the camera list, and the live card when one is picked. */
export function CountyPanel({
  county,
  verdict,
  cameras,
  onClose,
  onPickCamera,
  emptyText,
  sunAt = null,
  highwayKey = null,
  onPickHighway,
  children,
}: {
  county: string;
  verdict: CountyVerdict;
  cameras: { camera_id: string; name: string; route: string; hasStream?: boolean; action?: string }[];
  onClose: () => void;
  onPickCamera?: (cameraId: string) => void;
  emptyText?: string;
  /** Where to read the sun for the "Sunset 6:09 PM" line: one of the county's cameras. */
  sunAt?: { lat: number; lon: number } | null;
  /** The highway the list is narrowed to (a key from `highwayGroups`), or null for all. */
  highwayKey?: string | null;
  onPickHighway?: (key: string | null) => void;
  children?: React.ReactNode;
}) {
  const groups = highwayGroups(cameras.map((camera) => camera.route));
  const order = new Map(groups.map((group, index) => [group.key, index]));
  const groupOf = (route: string) => groups.find((group) => onHighway(route, group.key))?.key ?? '';
  // Grouped by highway (interstates first), and narrowed to one when it is picked.
  const listed = cameras
    .filter((camera) => !highwayKey || onHighway(camera.route, highwayKey))
    .map((camera, index) => ({ camera, index, group: order.get(groupOf(camera.route)) ?? 99 }))
    .sort((a, b) => a.group - b.group || a.index - b.index)
    .map((entry) => entry.camera);
  const shown = highwayKey ? listed : listed.slice(0, 6);
  const levelLabel = countyLevelLabel(verdict.level, verdict.cameras);
  const sun = useSunLine(sunAt);
  const countyColumn = (
    <>
      <div className="panel-head">
        <div>
          <span className="kicker">County · {verdict.cameras} {verdict.cameras === 1 ? 'camera' : 'cameras'}</span>
          <h2>{county}</h2>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close the county panel">
          ✕
        </button>
      </div>

      <div className="level-box">
        <span
          className="swatch swatch-lg"
          style={{ background: verdict.level === 'no_data' ? '#191C21' : LEVEL_COLOURS[verdict.level] }}
          aria-hidden="true"
        />
        <div>
          <span className="level-word">{levelLabel}</span>
          <span className="level-reason">{verdict.reason}</span>
        </div>
      </div>
      {sun ? <span className="sun-line">{sun}</span> : null}

      <div className="cam-list">
        <span className="kicker">Cameras</span>
        {groups.length > 1 && onPickHighway ? (
          <div className="highway-chips" role="group" aria-label="Filter cameras by highway">
            <button type="button" aria-pressed={!highwayKey} onClick={() => onPickHighway(null)}>
              All <span className="chip-count">{cameras.length}</span>
            </button>
            {groups.map((group) => (
              <button
                type="button"
                key={group.key}
                aria-pressed={highwayKey === group.key}
                onClick={() => onPickHighway(highwayKey === group.key ? null : group.key)}
                title={group.highway ? `Show ${group.label} cameras and highlight ${group.label} on the map` : undefined}
              >
                {group.label} <span className="chip-count">{group.count}</span>
              </button>
            ))}
          </div>
        ) : null}
        {cameras.length === 0 ? <p className="muted">{emptyText ?? 'No cameras swept here yet.'}</p> : null}
        {shown.map((camera) => (
          <button
            type="button"
            className="cam-row"
            key={camera.camera_id}
            onClick={() => onPickCamera?.(camera.camera_id)}
            disabled={!onPickCamera}
          >
            <span className="route-chip">{camera.route || '—'}</span>
            <span className="cam-name">{camera.name}</span>
            {camera.action ? <span className="cam-action">{camera.action}</span> : null}
          </button>
        ))}
        {listed.length > shown.length ? (
          <span className="muted">+ {listed.length - shown.length} more on the map</span>
        ) : null}
      </div>
    </>
  );
  // A picked camera takes most of the card: its panel on the left, the county beside it.
  if (children) {
    return (
      <aside className="county-panel camera-open" aria-label="County details">
        <div className="panel-split">
          <div className="panel-camera">{children}</div>
          <div className="panel-inner">{countyColumn}</div>
        </div>
      </aside>
    );
  }
  return (
    <aside className="county-panel" aria-label="County details">
      <div className="panel-inner">{countyColumn}</div>
    </aside>
  );
}

/** Worker health for the header pill: `/api/health` answers 200 when the worker is reachable. */
export function useWorkerOnline(): boolean | null {
  const [online, setOnline] = useState<boolean | null>(null);
  useEffect(() => {
    if (USE_MOCKS) {
      setOnline(true);
      return;
    }
    const controller = new AbortController();
    fetch('/api/health', { signal: controller.signal, headers: { accept: 'application/json' } })
      .then((response) => setOnline(response.ok))
      .catch(() => setOnline(false));
    return () => controller.abort();
  }, []);
  return online;
}
