/**
 * Highways: group a county's cameras by the road they watch, and pick that road's line out of
 * the basemap so the map can highlight all of it.
 *
 * Caltrans names routes "I-80", "US-50", "SR-99"; FL511 writes "SR 600/Gandy Blvd" or a
 * street name. The basemap (OpenMapTiles' `transportation_name` layer) tags a highway with a
 * `network` (us-interstate, us-highway, us-state) and a `ref`, the route number.
 */

export type Network = 'us-interstate' | 'us-highway' | 'us-state';

export type Highway = {
  /** "I-80", "US-50", "SR-99": also the label. */
  key: string;
  network: Network;
  /** The route number as the basemap writes it: "80", "50", "99", "4A". */
  ref: string;
  /** The street FL511 names after the slash, "Gandy" in "SR 600/Gandy Blvd", if any. */
  street?: string;
};

const PREFIXES: Record<string, { network: Network; label: string }> = {
  I: { network: 'us-interstate', label: 'I' },
  US: { network: 'us-highway', label: 'US' },
  SR: { network: 'us-state', label: 'SR' },
  CA: { network: 'us-state', label: 'SR' },
  FL: { network: 'us-state', label: 'SR' },
};

const ROUTE = /^\s*(I|US|SR|CA|FL)[\s-]*(\d{1,4}[A-Z]?)\b/i;
const STREET = /\/\s*(.+?)(?:\s+(?:Blvd|Boulevard|Ave|Avenue|St|Street|Rd|Road|Hwy|Highway|Pkwy|Parkway|Dr|Drive|Expy|Expressway))?\.?\s*$/i;

/** The highway a camera's route names, or null for a street or an unknown route. */
export function parseHighway(route: string | null | undefined): Highway | null {
  const match = ROUTE.exec(route ?? '');
  if (!match) return null;
  const prefix = PREFIXES[match[1].toUpperCase()];
  const ref = match[2].toUpperCase();
  const highway: Highway = { key: `${prefix.label}-${ref}`, network: prefix.network, ref };
  const street = STREET.exec(route ?? '')?.[1].trim();
  if (street) highway.street = street;
  return highway;
}

export type HighwayGroup = { key: string; highway: Highway | null; label: string; count: number };

/** OSM's route-relation codes for each network, as `route_N_network` carries them. */
const ROUTE_NETWORKS: Record<Network, string[]> = {
  'us-interstate': ['US:I'],
  'us-highway': ['US:US'],
  'us-state': ['US:CA', 'US:FL'],
};

/**
 * Routes the tiles know by another name. SR-51 is signed and mapped as Business 80 (the
 * Capital City Freeway); its state number is hidden. Where Business 80 shares the road with
 * US-50 west of the 51 junction it is not SR-51, so refs naming US 50 are left out.
 */
const ALSO_KNOWN_AS: Record<string, { network: string; ref: string; mainRef: string; notIn: string }> = {
  'SR-51': { network: 'US:I:Business:Loop', ref: '80', mainRef: 'I 80 Business', notIn: 'US 50' },
};

const NETWORK_ORDER: Record<Network, number> = { 'us-interstate': 0, 'us-highway': 1, 'us-state': 2 };
export const LOCAL_ROADS = 'local';

/**
 * The county's highways, busiest first within interstates, then US routes, then state
 * routes; cameras on streets go last under "Local roads".
 */
export function highwayGroups(routes: (string | null | undefined)[]): HighwayGroup[] {
  const groups = new Map<string, HighwayGroup>();
  for (const route of routes) {
    const highway = parseHighway(route);
    const key = highway?.key ?? LOCAL_ROADS;
    const group = groups.get(key) ?? { key, highway, label: highway?.key ?? 'Local roads', count: 0 };
    group.count += 1;
    groups.set(key, group);
  }
  const rank = (group: HighwayGroup) => (group.highway ? NETWORK_ORDER[group.highway.network] : 9);
  return [...groups.values()].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      b.count - a.count ||
      a.label.localeCompare(b.label, 'en', { numeric: true }),
  );
}

/** Does this camera's route belong to the chosen group? */
export function onHighway(route: string | null | undefined, groupKey: string): boolean {
  const highway = parseHighway(route);
  return (highway?.key ?? LOCAL_ROADS) === groupKey;
}

/**
 * A MapLibre filter for the highway's lines in `transportation_name`.
 *
 * The tiles tag a road twice. `network` + `ref` name its main route ("us-highway" + "50").
 * `route_1..6_network` + `route_N_ref` list every route on it with OSM's codes ("US:US" +
 * "50"); that is the only place US-50 appears where it shares the Capital City Freeway with
 * I-80 Business (main ref "US 50;I 80 Business"). Checked against the live tiles in
 * Sacramento on 2026-10-29. Business loops ("US:I:Business:Loop") are a different road and
 * deliberately don't match.
 *
 * Some state routes carry no number in the tiles at all: SR 600 on Gandy Boulevard in Tampa is
 * listed as "US:FL" with no ref. For those the street FL511 names ("Gandy") matches by name.
 */
export function highwayFilter(highway: Highway | null): unknown[] {
  if (!highway) return ['==', ['literal', 1], 0];
  const refIs = (field: string) => ['==', ['to-string', ['coalesce', ['get', field], '']], highway.ref];
  const routeIs = (n: number) => [
    'all',
    ['match', ['coalesce', ['get', `route_${n}_network`], ''], ROUTE_NETWORKS[highway.network], true, false],
    refIs(`route_${n}_ref`),
  ];
  return [
    'any',
    ['all', ['==', ['get', 'network'], highway.network], refIs('ref')],
    ...[1, 2, 3, 4, 5, 6].map(routeIs),
    ...(highway.street ? [['in', highway.street, ['coalesce', ['get', 'name'], '']]] : []),
    ...aliasFilter(highway.key),
  ];
}

/**
 * Fill the gaps: some stretches of a highway carry its network but no number (SR-99 through
 * south Sacramento is just "Golden State Highway"). Once the matched pieces are known, roads on
 * the same network with no ref and one of their names are the same highway.
 */
export function withNamedGaps(filter: unknown[], highway: Highway, names: string[]): unknown[] {
  if (names.length === 0) return filter;
  return [
    'any',
    filter,
    [
      'all',
      ['==', ['get', 'network'], highway.network],
      ['==', ['to-string', ['coalesce', ['get', 'ref'], '']], ''],
      ['match', ['to-string', ['coalesce', ['get', 'name'], '']], names, true, false],
    ],
  ];
}

function aliasFilter(key: string): unknown[][] {
  const alias = ALSO_KNOWN_AS[key];
  if (!alias) return [];
  const mainRef = ['to-string', ['coalesce', ['get', 'ref'], '']];
  const slot = (n: number) => [
    'all',
    ['==', ['get', `route_${n}_network`], alias.network],
    ['==', ['to-string', ['coalesce', ['get', `route_${n}_ref`], '']], alias.ref],
  ];
  return [
    [
      'all',
      ['!', ['in', alias.notIn, mainRef]],
      ['any', ['in', alias.mainRef, mainRef], ...[1, 2, 3, 4, 5, 6].map(slot)],
    ],
  ];
}
