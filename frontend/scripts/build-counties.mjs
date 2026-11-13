// Build the county shapes the map draws: California (FIPS 06) and Florida (12).
//
//   npm run build:counties
//
// Reads us-atlas/counties-10m.json (US Census cartographic boundaries, public domain),
// keeps the counties of the two states, rounds coordinates to 4 decimals (about 11 m, well
// under what a state-wide map can show) and writes GeoJSON with `name` and `fips`.
//
// The outputs are committed so the app does not need the 8 MB atlas at runtime or at build
// time; keep each file under about 300 KB.

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { feature } from 'topojson-client';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const atlasPath = resolve(root, 'node_modules/us-atlas/counties-10m.json');
const outDir = resolve(root, 'public/geo');

const STATES = [
  { fips: '06', file: 'counties-ca.json', name: 'California' },
  { fips: '12', file: 'counties-fl.json', name: 'Florida' },
];

const round = (value) => Math.round(value * 1e4) / 1e4;

function roundCoords(coords) {
  if (typeof coords[0] === 'number') return [round(coords[0]), round(coords[1])];
  return coords.map(roundCoords);
}

// us-atlas names counties "Sacramento County, California"; the map wants "Sacramento".
function tidyName(raw) {
  return raw.replace(/\s+County$/, '').replace(/,\s*[A-Za-z ]+$/, '');
}

const atlas = JSON.parse(await readFile(atlasPath, 'utf8'));
const counties = feature(atlas, atlas.objects.counties);

let failed = false;
await mkdir(outDir, { recursive: true });

for (const state of STATES) {
  const features = counties.features
    .filter((f) => String(f.id).padStart(5, '0').startsWith(state.fips))
    .map((f) => ({
      type: 'Feature',
      properties: { name: tidyName(f.properties.name), fips: String(f.id).padStart(5, '0') },
      geometry: { type: f.geometry.type, coordinates: roundCoords(f.geometry.coordinates) },
    }))
    .sort((a, b) => a.properties.name.localeCompare(b.properties.name));

  const collection = { type: 'FeatureCollection', features };
  const path = resolve(outDir, state.file);
  const text = JSON.stringify(collection);
  await writeFile(path, text + '\n', 'utf8');
  const kb = Math.round(Buffer.byteLength(text) / 1024);
  console.log(`${state.name}: ${features.length} counties -> public/geo/${state.file} (${kb} KB)`);
  if (kb > 300) {
    console.error(`  too big: ${kb} KB, over the 300 KB budget`);
    failed = true;
  }
}

if (failed) process.exit(1);
