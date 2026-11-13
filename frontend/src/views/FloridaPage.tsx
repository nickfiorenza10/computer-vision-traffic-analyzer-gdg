import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { CameraPanel } from '../components/CameraPanel';
import { StateMap, type MapController } from '../components/StateMap';
import { CountyPanel, Hero, LegendRow, Regions, StatTiles, type Region } from '../components/site';
import { fetchFl511Cameras } from '../api';
import { locateCameras, type CountyCollection, type CountyFeature } from '../lib/counties';
import { countyLevel } from '../lib/countyLevel';
import type { CountyDatum, MapMode } from '../lib/stateMap';
import { hashFor } from '../lib/routes';
import { highwayGroups } from '../lib/highways';
import type { Fl511Camera, MapCameraFeature } from '../types';

const GEO_URL = '/geo/counties-fl.json';
const TIME_ZONE = 'America/New_York';

/** FL511 cameras as map features, so the same map and panel code can draw them. */
function asMapFeatures(cameras: Fl511Camera[]): MapCameraFeature[] {
  return cameras.map((camera) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [camera.longitude, camera.latitude] },
    properties: {
      camera_id: `fl511-${camera.camera_id}`,
      name: camera.description,
      county: '',
      route: camera.road,
      direction: camera.direction,
      // Whether FL511 has video for it is the worker's to find out when it is asked.
      has_stream: true,
      image_url: camera.image_url,
      swept: false,
      level: null,
      vehicles_mean: null,
      swept_at: null,
    },
  }));
}

export function FloridaPage() {
  const [counties, setCounties] = useState<CountyFeature[] | null>(null);
  const [raw, setRaw] = useState<Fl511Camera[] | null>(null);
  const [mode, setMode] = useState<MapMode>('coverage');
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
    fetchFl511Cameras(abort.signal)
      .then((response) => setRaw(response.cameras))
      .catch(() => setError((current) => current ?? 'The Florida camera list did not load.'));
    return () => abort.abort();
  }, []);

  // Attach each camera's county, so the panel's camera list and the map agree.
  const cameras = useMemo(() => {
    if (!raw) return null;
    const features = asMapFeatures(raw);
    if (!counties) return features;
    const placed = locateCameras(
      features.map((feature) => ({
        id: feature.properties.camera_id,
        longitude: feature.geometry.coordinates[0],
        latitude: feature.geometry.coordinates[1],
      })),
      counties,
    );
    const byId = new Map(placed.map((entry) => [entry.id, entry.county]));
    return features.map((feature) => ({
      ...feature,
      properties: { ...feature.properties, county: byId.get(feature.properties.camera_id) ?? '' },
    }));
  }, [raw, counties]);
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
        // Florida has no sweeps yet: every camera is learning in the level mode, and the
        // coverage mode shows what is there.
        verdict: countyLevel(
          inside.map(() => ({ level: null, sweptAt: null })),
          now,
          TIME_ZONE,
        ),
      });
    }
    return map;
  }, [counties, cameras, now]);

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

  const picked = cameraId ? raw?.find((camera) => `fl511-${camera.camera_id}` === cameraId) ?? null : null;

  // A picked camera slides the map to a strip (0.55 s), then the map closes in on the camera
  // so its streets and their names show; closing it fits the county again.
  const pickedAt = picked ? `${picked.longitude},${picked.latitude}` : null;
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
  // The picked group's road, as the first of its cameras names it (FL511 adds the street).
  const highlight = useMemo(() => {
    if (!highwayKey) return null;
    const group = highwayGroups(countyCameras.map((camera) => camera.properties.route)).find((g) => g.key === highwayKey);
    return { key: highwayKey, highway: group?.highway ?? null };
  }, [countyCameras, highwayKey]);

  const verdict = selected ? data.get(selected)?.verdict ?? null : null;

  const regions: Region[] = [
    {
      name: 'Tampa Bay',
      tag: 'HILLSBOROUGH',
      description: 'Gandy Boulevard, Dale Mabry and the bay bridges. FL511 cameras, used with FDOT’s permission.',
      county: 'Hillsborough',
    },
    { name: 'California', tag: 'CWWP2', description: 'Caltrans cameras across the state, swept every 30 minutes.', href: hashFor('california'), note: 'Switch to California →' },
    { name: 'Pinellas', tag: 'FL511', description: 'St Petersburg and the beaches, from the FL511 camera list.' },
    { name: 'Orange & Osceola', tag: 'FL511', description: 'Orlando and the I-4 corridor.' },
    { name: 'Miami-Dade', tag: 'FL511', description: 'Miami, the Turnpike and the I-95 corridor.' },
  ];

  return (
    <main className="page">
      <Hero state="fl" />

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
              <span className="visually-hidden">Search counties</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && matches.length > 0) pick(matches[0]);
                }}
                placeholder="Search counties, e.g. Hillsborough"
                aria-label="Search counties"
              />
            </label>
            <span className="badge">{raw ? `${raw.length} cameras · Florida` : '…'}</span>
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
              state="fl"
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
            County outlines: US Census · Cameras: FL511, with FDOT’s permission · Map: OpenStreetMap
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
              action: 'Analyze live',
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
            emptyText="No cameras here yet."
          >
            {picked ? (
              <CameraPanel
                key={picked.camera_id}
                camera={{
                  ...asMapFeatures([picked])[0],
                  properties: { ...asMapFeatures([picked])[0].properties, county: selected },
                }}
                onClose={() => setCameraId(null)}
              />
            ) : null}
          </CountyPanel>
        ) : null}
      </section>

      <LegendRow mode={mode} />
      <StatTiles cameras={cameras?.length ?? 0} swept={0} />
      <Regions regions={regions} onPick={pick} />
    </main>
  );
}
