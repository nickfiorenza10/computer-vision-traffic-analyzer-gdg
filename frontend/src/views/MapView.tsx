import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, fetchMapCameras } from '../api';
import { CameraMap } from '../components/CameraMap';
import { CameraPanel } from '../components/CameraPanel';
import { MapLegend } from '../components/MapLegend';
import { Empty, ErrorState, Loading } from '../components/StateViews';
import { anyTrafficReadings, tooltipLines, type MapMode } from '../lib/map';
import type { MapCamerasResponse } from '../types';

/**
 * The main page: California's cameras on a map, heat coloured by traffic level or by camera
 * coverage, and a panel that analyses a camera live when it is clicked.
 */
export function MapView() {
  const [cameras, setCameras] = useState<MapCamerasResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<MapMode>('level');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchMapCameras(controller.signal)
      .then((response) => {
        setCameras(response);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return;
        setLoading(false);
        setError(reason instanceof ApiError ? reason.message : 'The camera map could not be loaded.');
      });
    return () => controller.abort();
  }, [reloadKey]);

  const counts = useMemo(() => {
    const tally: Record<string, number> = { heavy: 0, moderate: 0, light: 0, learning: 0, night: 0, none: 0 };
    for (const feature of cameras?.features ?? []) {
      const level = feature.properties.level;
      tally[level ?? 'none'] = (tally[level ?? 'none'] ?? 0) + 1;
    }
    return tally;
  }, [cameras]);

  const selected = useMemo(
    () => cameras?.features.find((feature) => feature.properties.camera_id === selectedId) ?? null,
    [cameras, selectedId],
  );
  const hovered = useMemo(
    () => cameras?.features.find((feature) => feature.properties.camera_id === hoveredId) ?? null,
    [cameras, hoveredId],
  );

  const close = useCallback(() => setSelectedId(null), []);
  // A district where every camera is still learning or in the dark has nothing to colour,
  // and an empty map without a word reads as a broken map.
  const noReadings = useMemo(() => !!cameras && !anyTrafficReadings(cameras.features), [cameras]);

  // Escape closes the panel, which also tells the worker to stop the session.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <section className={`map-view${selected ? ' with-panel' : ''}`}>
      <div className="map-stage">
        {loading ? <Loading label="Loading the camera map…" /> : null}
        {error ? <ErrorState message={error} onRetry={() => setReloadKey((value) => value + 1)} /> : null}
        {!loading && !error && !cameras?.features.length ? (
          <Empty
            title="No cameras on the map yet."
            detail="The catalogue is refreshed daily by the worker; until then there is nothing to show."
          />
        ) : null}
        {cameras ? (
          <CameraMap
            cameras={cameras}
            mode={mode}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onHover={setHoveredId}
          />
        ) : null}

        <MapLegend mode={mode} onModeChange={setMode} counts={counts} total={cameras?.features.length ?? 0} />

        {mode === 'level' && noReadings ? (
          <p className="map-note">
            No traffic readings yet: cameras are learning or it's dark.
          </p>
        ) : null}

        {hovered && !selected ? (
          <div className="map-tooltip" role="status">
            {tooltipLines(hovered.properties).map((line, index) => (
              <div key={line} className={index === 0 ? 'strong' : index === 1 ? 'sub' : undefined}>
                {line}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {selected ? <CameraPanel key={selected.properties.camera_id} camera={selected} onClose={close} /> : null}
    </section>
  );
}
