import { useEffect, useRef } from 'react';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import '../lib/mapWorker';

import type { CountyFeature } from '../lib/counties';
import { basemapPaint, camerasGeoJson, countyCoordinates, countiesGeoJson, boundsFor, mapStyle } from '../lib/stateMap';
import type { CountyDatum, DotGeo, MapMode } from '../lib/stateMap';
import { highwayFilter, onHighway, withNamedGaps, type Highway } from '../lib/highways';
import type { MapCameraFeature } from '../types';

const BASEMAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty';
const COUNTY_OPACITY = ['interpolate', ['linear'], ['zoom'], 8.5, 1, 10, 0.35] as const;

type StyleLayer = {
  id: string;
  type: string;
  source?: string;
  'source-layer'?: string;
  paint?: Record<string, unknown>;
  layout?: Record<string, unknown>;
  minzoom?: number;
  maxzoom?: number;
};

/**
 * Load OpenFreeMap's Liberty style and lift its sources plus the road and water layers in
 * below everything the map already has. Labels (symbol layers) are skipped on purpose.
 * A failed fetch is not fatal: the counties still draw.
 */
async function addBasemap(instance: maplibregl.Map): Promise<void> {
  try {
    const response = await fetch(BASEMAP_STYLE, { headers: { accept: 'application/json' } });
    if (!response.ok) return;
    const style = (await response.json()) as { sources?: Record<string, unknown>; layers?: StyleLayer[] };
    for (const [name, source] of Object.entries(style.sources ?? {})) {
      if (!instance.getSource(name)) instance.addSource(name, source as never);
    }
    const wanted = new Set(['transportation', 'water', 'waterway']);
    const added: StyleLayer[] = [];
    for (const layer of style.layers ?? []) {
      if (layer.type === 'symbol' || !layer['source-layer'] || !wanted.has(layer['source-layer'])) continue;
      if (!layer.source) continue;
      const id = `base-${layer.id}`;
      if (instance.getLayer(id)) continue;
      // Liberty's own palette (tan motorways, blue water) fights the dark theme and the level
      // colours, so the colours are replaced and any texture pattern dropped.
      const paint: Record<string, unknown> = { ...(layer.paint ?? {}), ...(basemapPaint(layer) ?? {}) };
      delete paint['fill-pattern'];
      delete paint['line-pattern'];
      const opacityKey = layer.type === 'line' ? 'line-opacity' : 'fill-opacity';
      const base = typeof paint[opacityKey] === 'number' ? (paint[opacityKey] as number) : 1;
      paint[opacityKey] = [
        'interpolate',
        ['linear'],
        ['zoom'],
        8.5,
        0,
        10,
        0.55 * base,
      ];
      instance.addLayer(
        {
          id,
          type: layer.type,
          source: layer.source,
          'source-layer': layer['source-layer'],
          paint,
          layout: { ...(layer.layout ?? {}), visibility: 'visible' },
          minzoom: layer.minzoom,
          maxzoom: layer.maxzoom,
        } as never,
        // Below the counties, not below the background: adding before the background layer
        // hides everything for good.
        FILL_LAYER,
      );
      added.push(layer);
    }
    // Counties read through the roads once zoomed in.
    instance.setPaintProperty(FILL_LAYER, 'fill-opacity', COUNTY_OPACITY as never);
  } catch {
    // Offline, blocked, or a changed style: counties only, which is a usable map.
  }
}

export type MapController = {
  zoomIn: () => void;
  zoomOut: () => void;
  reset: () => void;
  fitCounty: (name: string) => void;
  /** Close in on one camera, at a zoom where the streets around it carry their names. */
  focusCamera: (longitude: number, latitude: number) => void;
};

type Props = {
  state: 'ca' | 'fl';
  counties: CountyFeature[];
  cameras: MapCameraFeature[];
  data: Map<string, CountyDatum>;
  mode: MapMode;
  selected: string | null;
  onPickCounty: (name: string | null) => void;
  onPickCamera: (cameraId: string) => void;
  /** The highway picked in the county panel: its whole line is highlighted, its cameras lit. */
  highway?: { key: string; highway: Highway | null } | null;
  /** The camera open in the panel: its dot pulses. */
  pickedCameraId?: string | null;
  controllerRef?: { current: MapController | null };
};

const COUNTY_SOURCE = 'counties';
const FILL_LAYER = 'county-fill';
const LINE_LAYER = 'county-line';
const DOTS_LAYER = 'camera-dots';
const HIGHWAY_LAYER = 'highway-highlight';
const LABEL_LAYER = 'road-labels';
const PICK_RING = 'picked-ring';
const PICK_DOT = 'picked-dot';
const PULSE_MS = 1600;

/**
 * Road names from the zoom the roads fade in (8.5), not Liberty's 12-15: "I-80", "US-50",
 * "SR-99" on highways, the street name everywhere else. The tiles only carry the bigger
 * roads at low zoom, so small streets get their names as they appear.
 */
const REF = ['to-string', ['coalesce', ['get', 'ref'], '']];
// A shared stretch already spells its ref out ("US 50"), so it is shown as written.
const refWith = (prefix: string) => ['case', ['in', ' ', REF], REF, ['concat', prefix, REF]];
const ROAD_LABEL = [
  'case',
  ['==', ['get', 'network'], 'us-interstate'], refWith('I-'),
  ['==', ['get', 'network'], 'us-highway'], refWith('US-'),
  ['==', ['get', 'network'], 'us-state'], refWith('SR-'),
  ['coalesce', ['get', 'name_en'], ['get', 'name'], ''],
];
const ACCENT = '#3DDC97';
const DOT = 'rgba(242,243,245,0.75)';

/** Dots on the picked highway in the accent and a little larger; the rest dimmed. */
function dotPaint(key: string | null) {
  if (!key) {
    return { colour: DOT, radius: ['interpolate', ['linear'], ['zoom'], 4, 1.6, 10, 3] };
  }
  const on = ['==', ['get', 'highway'], key];
  return {
    colour: ['case', on, ACCENT, 'rgba(242,243,245,0.3)'],
    radius: ['interpolate', ['linear'], ['zoom'], 4, ['case', on, 2.6, 1.4], 10, ['case', on, 5, 2.6]],
  };
}

/**
 * The county map: MapLibre for real geography and real zoom, no basemap, counties coloured by
 * level or by camera count, cameras as small light dots.
 */
export function StateMap({
  state,
  counties,
  cameras,
  data,
  mode,
  selected,
  onPickCounty,
  onPickCamera,
  highway = null,
  pickedCameraId = null,
  controllerRef,
}: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const ready = useRef(false);
  const latest = useRef({ counties, data, mode, selected, state, cameras, highway });

  latest.current = { counties, data, mode, selected, state, cameras, highway };

  const countyBounds = (name: string): [[number, number], [number, number]] | null => {
    const county = latest.current.counties.find((entry) => entry.properties.name === name);
    if (!county) return null;
    return boundsFor(countyCoordinates([county]), 0.06);
  };

  const fitState = () => {
    const instance = map.current;
    const bounds = boundsFor(countyCoordinates(latest.current.counties), 0.3);
    if (!instance || !bounds) return;
    instance.fitBounds(bounds, { padding: 24, duration: 500 });
  };

  // Create the map once.
  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: mapStyle() as never,
      bounds: boundsFor(countyCoordinates(counties), 0.3) ?? undefined,
      fitBoundsOptions: { padding: 24 },
      attributionControl: false,
      dragRotate: false,
    });
    map.current = instance;

    // The card is laid out after the map is created (the panel opens, the window changes), so
    // the canvas must follow its container or it draws into the old size.
    const observer = new ResizeObserver(() => instance.resize());
    if (container.current) observer.observe(container.current);

    instance.on('load', () => {
      instance.addSource(COUNTY_SOURCE, {
        type: 'geojson',
        data: countiesGeoJson(latest.current.counties, latest.current.data, latest.current.mode) as never,
      });
      instance.addLayer({
        id: FILL_LAYER,
        type: 'fill',
        source: COUNTY_SOURCE,
        paint: { 'fill-color': ['get', 'fill'], 'fill-opacity': COUNTY_OPACITY as never },
      });
      instance.addLayer({
        id: LINE_LAYER,
        type: 'line',
        source: COUNTY_SOURCE,
        paint: {
          'line-color': ['case', ['==', ['get', 'name'], latest.current.selected ?? ''], '#F2F3F5', '#0B0C0E'],
          'line-width': ['case', ['==', ['get', 'name'], latest.current.selected ?? ''], 2, 0.7],
        },
      });
      instance.addSource('camera-dots', { type: 'geojson', data: camerasGeoJson(latest.current.cameras) as never });
      instance.addLayer({
        id: DOTS_LAYER,
        type: 'circle',
        source: 'camera-dots',
        paint: {
          'circle-color': dotPaint(latest.current.highway?.key ?? null).colour as never,
          'circle-radius': dotPaint(latest.current.highway?.key ?? null).radius as never,
        },
      });

      // The picked camera: a solid dot with a ring that grows and fades, over the other dots.
      const pickFilter = ['==', ['get', 'camera_id'], ''];
      instance.addLayer({
        id: PICK_RING,
        type: 'circle',
        source: 'camera-dots',
        filter: pickFilter as never,
        paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': ACCENT, 'circle-stroke-width': 2, 'circle-radius': 6 },
      });
      instance.addLayer({
        id: PICK_DOT,
        type: 'circle',
        source: 'camera-dots',
        filter: pickFilter as never,
        paint: { 'circle-color': ACCENT, 'circle-radius': 6, 'circle-stroke-color': '#0B0C0E', 'circle-stroke-width': 2 },
      });

      instance.on('click', FILL_LAYER, (event: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
        const name = event.features?.[0]?.properties?.name as string | undefined;
        if (!name) return;
        onPickCounty(name);
        const bounds = countyBounds(name);
        if (bounds) instance.fitBounds(bounds, { padding: 48, duration: 600 });
      });
      instance.on('click', DOTS_LAYER, (event: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) => {
        const id = event.features?.[0]?.properties?.camera_id as string | undefined;
        if (id) onPickCamera(id);
      });
      for (const layer of [FILL_LAYER, DOTS_LAYER]) {
        instance.on('mouseenter', layer, () => {
          instance.getCanvas().style.cursor = 'pointer';
        });
        instance.on('mouseleave', layer, () => {
          instance.getCanvas().style.cursor = '';
        });
      }

      // The basemap: OpenFreeMap's own sources and its road and water layers, beneath the
      // counties, faded in from zoom 8.5 and labels left out (they would fight the panel).
      // The highway highlight reads the same tiles, so it waits for them; it sits above the
      // counties and below the dots, and shows at every zoom.
      void addBasemap(instance).then(() => {
        if (!instance.getSource('openmaptiles') || instance.getLayer(HIGHWAY_LAYER)) return;
        instance.addLayer(
          {
            id: HIGHWAY_LAYER,
            type: 'line',
            source: 'openmaptiles',
            'source-layer': 'transportation_name',
            filter: highwayFilter(latest.current.highway?.highway ?? null) as never,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: {
              'line-color': ACCENT,
              'line-opacity': 0.9,
              'line-width': ['interpolate', ['linear'], ['zoom'], 5, 2, 10, 4, 14, 7] as never,
            },
          },
          DOTS_LAYER,
        );
        instance.addLayer(
          {
            id: LABEL_LAYER,
            type: 'symbol',
            source: 'openmaptiles',
            'source-layer': 'transportation_name',
            minzoom: 8.5,
            layout: {
              'symbol-placement': 'line',
              'text-field': ROAD_LABEL as never,
              'text-font': ['Noto Sans Regular'],
              'text-size': ['interpolate', ['linear'], ['zoom'], 8.5, 11, 12, 13, 15, 14] as never,
              'text-letter-spacing': 0.02,
              'symbol-spacing': 280,
              'text-max-angle': 30,
            },
            paint: {
              'text-color': '#C9CDD3',
              'text-halo-color': '#0B0C0E',
              'text-halo-width': 1.4,
              'text-opacity': ['interpolate', ['linear'], ['zoom'], 8.5, 0, 9.5, 0.9] as never,
            },
          },
          DOTS_LAYER,
        );
      });

      ready.current = true;
    });

    controllerRef!.current = {
      zoomIn: () => map.current?.zoomIn({ duration: 250 }),
      zoomOut: () => map.current?.zoomOut({ duration: 250 }),
      reset: () => {
        onPickCounty(null);
        fitState();
      },
      fitCounty: (name) => {
        const bounds = countyBounds(name);
        const instance = map.current;
        if (!bounds || !instance) return;
        // The panel changes the card's width; fit against the size it will have, not the one
        // it had when the click landed.
        instance.resize();
        instance.fitBounds(bounds, { padding: 40, duration: 600 });
      },
      focusCamera: (longitude, latitude) => {
        const instance = map.current;
        if (!instance) return;
        instance.resize();
        instance.easeTo({ center: [longitude, latitude], zoom: Math.max(instance.getZoom(), 12.5), duration: 700 });
      },
    };

    return () => {
      observer.disconnect();
      map.current?.remove();
      map.current = null;
      ready.current = false;
    };
    // The map is created once; everything else updates through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // New data or the other mode: repaint the county fills.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    const source = instance.getSource(COUNTY_SOURCE) as maplibregl.GeoJSONSource | undefined;
    source?.setData(countiesGeoJson(counties, data, mode) as never);
  }, [counties, data, mode]);

  // New cameras.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    const source = instance.getSource('camera-dots') as maplibregl.GeoJSONSource | undefined;
    source?.setData(camerasGeoJson(cameras) as never);
  }, [cameras]);

  // The picked highway: its line in the basemap, and its cameras.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    const base = highwayFilter(highway?.highway ?? null);
    if (instance.getLayer(HIGHWAY_LAYER)) instance.setFilter(HIGHWAY_LAYER, base as never);
    // As tiles load, learn the matched road's names and light its unnumbered stretches too.
    const names = new Set<string>();
    const learnNames = () => {
      const road = highway?.highway;
      if (!road || !instance.getLayer(HIGHWAY_LAYER)) return;
      const before = names.size;
      for (const feature of instance.querySourceFeatures('openmaptiles', {
        sourceLayer: 'transportation_name',
        filter: base as never,
      })) {
        const name = feature.properties?.name;
        if (typeof name === 'string' && name) names.add(name);
      }
      if (names.size > before) instance.setFilter(HIGHWAY_LAYER, withNamedGaps(base, road, [...names]) as never);
    };
    if (highway?.highway) {
      instance.on('idle', learnNames);
      learnNames();
    }
    const paint = dotPaint(highway?.key ?? null);
    instance.setPaintProperty(DOTS_LAYER, 'circle-color', paint.colour as never);
    instance.setPaintProperty(DOTS_LAYER, 'circle-radius', paint.radius as never);
    if (!highway) return;
    // Frame the highway's cameras in the county. The basemap's tiles below zoom 10 leave out
    // stretches of a highway's name layer (US-50 through Sacramento), so stay at 10 or closer.
    const { cameras: all, selected: county } = latest.current;
    const bounds = boundsFor(
      all
        .filter((camera) => (!county || camera.properties.county === county) && onHighway(camera.properties.route, highway.key))
        .map((camera) => camera.geometry.coordinates as [number, number]),
      0.05,
    );
    const view = bounds ? instance.cameraForBounds(bounds, { padding: 48 }) : undefined;
    if (view) instance.easeTo({ ...view, zoom: Math.min(12, Math.max(10, view.zoom ?? 10)), duration: 600 });
    return () => {
      instance.off('idle', learnNames);
    };
  }, [highway]);

  // The picked camera's pulse: one ring every 1.6 s while a camera is open.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current || !instance.getLayer(PICK_RING)) return;
    const filter = ['==', ['get', 'camera_id'], pickedCameraId ?? ''];
    instance.setFilter(PICK_RING, filter as never);
    instance.setFilter(PICK_DOT, filter as never);
    if (!pickedCameraId) return;
    let frame = 0;
    const tick = (now: number) => {
      const t = (now % PULSE_MS) / PULSE_MS;
      instance.setPaintProperty(PICK_RING, 'circle-radius', 6 + 18 * t);
      instance.setPaintProperty(PICK_RING, 'circle-stroke-opacity', 1 - t);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [pickedCameraId]);

  // Selection outline.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready.current) return;
    instance.setPaintProperty(LINE_LAYER, 'line-color', [
      'case',
      ['==', ['get', 'name'], selected ?? ''],
      '#F2F3F5',
      '#0B0C0E',
    ]);
    instance.setPaintProperty(LINE_LAYER, 'line-width', [
      'case',
      ['==', ['get', 'name'], selected ?? ''],
      2,
      0.7,
    ]);
  }, [selected]);

  // The other state: fit its counties.
  useEffect(() => {
    if (!ready.current) return;
    fitState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, counties]);

  return <div className="state-map" ref={container} aria-label="Map of counties and traffic cameras" />;
}

export type { DotGeo, MapMode };
