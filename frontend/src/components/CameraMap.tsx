import { useEffect, useRef } from 'react';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import '../lib/mapWorker';
import type { MapCamerasResponse } from '../types';
import { CALIFORNIA_BOUNDS, type MapMode } from '../lib/map';
import {
  ALL_LAYERS,
  CAMERAS_SOURCE,
  GLOW_LAYER,
  HEAT_LAYER,
  MAP_STYLE,
  POINT_LAYER,
  cameraSource,
  visibleLayers,
} from '../lib/mapLayers';

type Props = {
  cameras: MapCamerasResponse | null;
  mode: MapMode;
  selectedId: string | null;
  onSelect: (cameraId: string) => void;
  onHover: (cameraId: string | null) => void;
};

/**
 * The map itself. MapLibre draws the heat and the points; React owns the data and the
 * selection. OpenFreeMap's vector tiles need no key, and the attribution stays visible.
 */
export function CameraMap({ cameras, mode, selectedId, onSelect, onHover }: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const ready = useRef(false);
  const latest = useRef({ cameras, mode, onSelect, onHover });
  latest.current = { cameras, mode, onSelect, onHover };

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: MAP_STYLE,
      bounds: CALIFORNIA_BOUNDS,
      fitBoundsOptions: { padding: 32 },
      attributionControl: { compact: true },
    });
    map.current = instance;
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

    instance.on('load', () => {
      const { cameras: current, mode: currentMode } = latest.current;
      // The construction-time bounds are applied before the container has its final size,
      // so the first view lands off-centre. Fit again now that it is measured.
      instance.resize();
      instance.fitBounds(CALIFORNIA_BOUNDS, { padding: 32, duration: 0 });
      // Development only: lets a browser check or an end-to-end test drive the map.
      if (import.meta.env.DEV) {
        (window as unknown as { __map?: maplibregl.Map }).__map = instance;
      }
      instance.addSource(CAMERAS_SOURCE, cameraSource(current ?? { type: 'FeatureCollection', features: [] }, currentMode));
      for (const layer of ALL_LAYERS) instance.addLayer(layer);
      // Both wash layers exist from the start; the mode only flips their visibility, so
      // switching never re-adds a layer (and never flashes an empty map).
      for (const id of [HEAT_LAYER, GLOW_LAYER]) {
        instance.setLayoutProperty(id, 'visibility', visibleLayers(currentMode).includes(id) ? 'visible' : 'none');
      }
      ready.current = true;

      instance.on('click', POINT_LAYER, (event: maplibregl.MapLayerMouseEvent) => {
        const id = event.features?.[0]?.properties?.id;
        if (typeof id === 'string') latest.current.onSelect(id);
      });
      instance.on('mouseenter', POINT_LAYER, () => {
        instance.getCanvas().style.cursor = 'pointer';
      });
      instance.on('mouseleave', POINT_LAYER, () => {
        instance.getCanvas().style.cursor = '';
      });
      instance.on('mousemove', POINT_LAYER, (event: maplibregl.MapLayerMouseEvent) => {
        const id = event.features?.[0]?.properties?.id;
        latest.current.onHover(typeof id === 'string' ? id : null);
      });
      instance.on('mouseleave', POINT_LAYER, () => latest.current.onHover(null));
    });

    return () => {
      instance.remove();
      map.current = null;
      ready.current = false;
    };
  }, []);

  // New data, or the other mode: replace the source and switch which wash is drawn.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current || !cameras) return;
    const source = instance.getSource(CAMERAS_SOURCE) as maplibregl.GeoJSONSource | undefined;
    source?.setData(cameraSource(cameras, mode).data as never);
    const visible = visibleLayers(mode);
    for (const id of [HEAT_LAYER, GLOW_LAYER]) {
      if (instance.getLayer(id)) {
        instance.setLayoutProperty(id, 'visibility', visible.includes(id) ? 'visible' : 'none');
      }
    }
  }, [cameras, mode]);

  // Selecting a camera flies to it; clearing the selection leaves the view alone.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current || !cameras || !selectedId) return;
    const feature = cameras.features.find((entry) => entry.properties.camera_id === selectedId);
    if (!feature) return;
    const [longitude, latitude] = feature.geometry.coordinates;
    instance.flyTo({ center: [longitude, latitude], zoom: 13, speed: 1.2, curve: 1.5 });
  }, [selectedId, cameras]);

  return <div className="map-canvas" ref={container} data-testid="camera-map" />;
}
