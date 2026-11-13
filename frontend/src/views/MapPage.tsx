import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { CameraPanel } from '../components/CameraPanel';
import { StateMap, type MapController } from '../components/StateMap';
import { CountyPanel, Hero, LegendRow, Regions, StatTiles, type Region } from '../components/site';
import { fetchMapCameras } from '../api';
import { locateCameras, type CountyCollection, type CountyFeature } from '../lib/counties';
import { countyLevel } from '../lib/countyLevel';
import { countiesGeoJson, type CountyDatum, type MapMode } from '../lib/stateMap';
import { hashFor } from '../lib/routes';
import { highwayGroups } from '../lib/highways';
import type { MapCameraFeature } from '../types';

const GEO_URL = '/geo/counties-ca.json';
const TIME_ZONE = 'America/Los_Angeles';

export function MapPage() {
  const [counties, setCounties] = useState<CountyFeature[] | null>(null);
  const [cameras, setCameras] = useState<MapCameraFeature[] | null>(null);
  const [mode, setMode] = useState<MapMode>('level');
  const [selected, setSelected] = useState<string | null>(null);
  const [cameraId, setCameraId] = useState<string | null>(null);
  const [highwayKey, setHighwayKey] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<MapController | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    fetch(GEO_URL, { signal: abort.signal })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then((geo: CountyCollection) => setCounties(geo.features))
      .catch(() => setError('The county shapes did not load.'));
    fetchMapCameras(abort.signal)
      .then((response) => setCameras(response.features))
      .catch(() => setError((current) => current ?? 'The camera list did not load.'));
    return () => abort.abort();
  }, []);

  const now = Date.now();

  const data = useMemo(() => {
    const map = new Map<string, CountyDatum>();
    if (!counties || !cameras) return map;
    const placed = locateCameras(
      cameras.map((camera) => ({
        id: camera.properties.camera_id,
        longitude: camera.geometry.coordinates[0],
        latitude: camera.geometry.coordinates[1],
      })),
      counties,
    );
    for (const county of counties) {
      const inside = placed.filter((camera) => camera.county === county.properties.name);
      map.set(county.properties.name, {
        name: county.properties.name,
        cameras: inside.length,
        verdict: countyLevel(
          inside.map((camera) => {
            const feature = cameras.find((entry) => entry.properties.camera_id === camera.id);
            return { level: feature?.properties.level ?? null, sweptAt: feature?.properties.swept_at ?? null };
          }),
          now,
          TIME_ZONE,
        ),
      });
    }
    return map;
  }, [counties, cameras, now]);

  const verdicts = useMemo(() => {
    let swept = 0;
    for (const datum of data.values()) if (datum.verdict.readings > 0) swept += 1;
    return { swept };
  }, [data]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!counties || needle.length < 2) return [];
    return counties
      .filter((county) => county.properties.name.toLowerCase().includes(needle))
      .map((county) => county.properties.name)
      .slice(0, 5);
  }, [counties, query]);

  const pick = useCallback((name: string | null) => {
    setSelected(name);
    setHighwayKey(null);
    setCameraId(null);
    if (!name) return;
    // The panel animates open over 0.5 s; fitting before it finishes would fit the old width
    // and clip the county behind the panel.
    window.setTimeout(() => controller.current?.fitCounty(name), 560);
  }, []);

  const close = useCallback(() => {
    setSelected(null);
    setHighwayKey(null);
    setCameraId(null);
    controller.current?.reset();
  }, []);


  const countyCameras = useMemo(() => {
    if (!selected || !cameras) return [] as MapCameraFeature[];
    return cameras.filter((camera) => camera.properties.county === selected);
  }, [cameras, selected]);

  // The picked group's road, as the first of its cameras names it (FL511 adds the street).
  const highlight = useMemo(() => {
    if (!highwayKey) return null;
    const group = highwayGroups(countyCameras.map((camera) => camera.properties.route)).find((g) => g.key === highwayKey);
    return { key: highwayKey, highway: group?.highway ?? null };
  }, [countyCameras, highwayKey]);

  const verdict = selected ? data.get(selected)?.verdict ?? null : null;
  const picked = cameraId ? cameras?.find((camera) => camera.properties.camera_id === cameraId) ?? null : null;

  // A picked camera slides the map to a strip (0.55 s), then the map closes in on the camera
  // so its streets and their names show; closing it fits the county again.
  const pickedAt = picked ? `${picked.geometry.coordinates[0]},${picked.geometry.coordinates[1]}` : null;
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (pickedAt) {
        const [longitude, latitude] = pickedAt.split(',').map(Number);
        controller.current?.focusCamera(longitude, latitude);
      } else if (selected) {
        controller.current?.fitCounty(selected);
      }
    }, 600);
    return () => window.clearTimeout(timer);
  }, [pickedAt]); // eslint-disable-line react-hooks/exhaustive-deps
  const badge = cameras ? `${cameras.length} cameras · California` : '…';

  const regions: Region[] = [
    {
      name: 'Sacramento Valley',
      tag: 'DISTRICT 3',
      description: 'Sacramento, Placer, El Dorado, Yolo and the valley north to Chico. Swept every 30 minutes.',
      county: 'Sacramento',
    },
    { name: 'Bay Area', tag: 'DISTRICT 4', description: 'San Francisco, Oakland, San Jose and the bridges.' },
    { name: 'Los Angeles & Ventura', tag: 'DISTRICT 7', description: 'The I-5, I-10, I-405 and US-101 corridors.' },
    { name: 'San Diego & Imperial', tag: 'DISTRICT 11', description: 'I-5 and I-15 from the border to Escondido.' },
    { name: 'Central Valley', tag: 'DISTRICTS 6, 10', description: 'Fresno, Stockton, Modesto and the CA-99 corridor.' },
    {
      name: 'Tampa Bay, Florida',
      tag: 'FL511',
      description: 'Hillsborough County cameras from FL511, used with FDOT’s permission.',
      href: hashFor('florida'),
      note: 'Switch to Florida →',
    },
  ];

  return (
    <main className="page">
      <Hero state="ca" />

      <section className="map-card" aria-label="Camera map">
        <div className="map-side">
          <div className="map-overlay-top">
            <div role="group" aria-label="Map mode" className="segmented">
              <button type="button" aria-pressed={mode === 'level'} onClick={() => setMode('level')}>
                Traffic level
              </button>
              <button type="button" aria-pressed={mode === 'coverage'} onClick={() => setMode('coverage')}>
                Coverage
              </button>
            </div>
            <label className="search">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#8A9099" strokeWidth="2" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-3.5-3.5" />
              </svg>
              <span className="visually-hidden">Search counties</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && matches.length > 0) pick(matches[0]);
                }}
                placeholder="Search counties, e.g. Placer"
                aria-label="Search counties"
              />
            </label>
            <span className="badge">{badge}</span>
          </div>

          <div className="map-zoom">
            <button type="button" aria-label="Zoom in" onClick={() => controller.current?.zoomIn()}>+</button>
            <button type="button" aria-label="Zoom out" onClick={() => controller.current?.zoomOut()}>−</button>
            <button type="button" aria-label="Reset the map" onClick={() => controller.current?.reset()}>⟳</button>
          </div>

          {matches.length > 0 && !selected ? (
            <ul className="match-list">
              {matches.map((name) => (
                <li key={name}>
                  <button type="button" onClick={() => pick(name)}>
                    {name}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          {counties && cameras ? (
            <StateMap
              state="ca"
              counties={counties}
              cameras={cameras}
              data={data}
              mode={mode}
              selected={selected}
              onPickCounty={pick}
              onPickCamera={(id) => {
                const camera = cameras.find((entry) => entry.properties.camera_id === id);
                if (!camera) return;
                setSelected(camera.properties.county);
                setCameraId(id);
              }}
              highway={highlight}
              pickedCameraId={cameraId}
              controllerRef={controller}
            />
          ) : (
            <div className="map-loading">{error ?? 'Loading the map…'}</div>
          )}

          <div className="map-attribution">
            County outlines: US Census · Cameras: Caltrans · Map: OpenStreetMap
          </div>
        </div>

        {selected && verdict ? (
          <CountyPanel
            county={selected}
            verdict={verdict}
            cameras={countyCameras.map((camera) => ({
              camera_id: camera.properties.camera_id,
              name: camera.properties.name,
              route: camera.properties.route,
              action: camera.properties.has_stream ? 'Analyze live' : 'No stream',
            }))}
            onClose={close}
            onPickCamera={(id) => setCameraId(id)}
            highwayKey={highwayKey}
            onPickHighway={setHighwayKey}
            sunAt={
              countyCameras[0]
                ? {
                    lat: countyCameras[0].geometry.coordinates[1],
                    lon: countyCameras[0].geometry.coordinates[0],
                  }
                : null
            }
          >
            {picked ? <CameraPanel camera={picked} onClose={() => setCameraId(null)} /> : null}
          </CountyPanel>
        ) : null}
      </section>

      <LegendRow mode={mode} />
      <StatTiles cameras={cameras?.length ?? 0} swept={verdicts.swept} />
      <Regions regions={regions} onPick={pick} />

      {/* Keep the county list in the bundle for the shapes check: one line, no render. */}
      <span className="visually-hidden">{countiesGeoJson(counties ?? [], data, mode).features.length} counties</span>
    </main>
  );
}
