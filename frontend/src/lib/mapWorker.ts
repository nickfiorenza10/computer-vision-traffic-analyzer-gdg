/**
 * MapLibre 6 loads its tile parser from a separate worker module. Vite resolves that
 * itself only sometimes — in development it fails with "Worker failed to load", and the map
 * then draws the style background but never fires `load` or draws a layer. Asking Vite for
 * the worker's URL (the `?url` suffix) makes it an asset in both dev and build, and handing
 * that to MapLibre is the one supported way to tell it where the worker lives.
 */

import { setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';

setWorkerUrl(workerUrl);
