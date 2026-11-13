/** The two states, the charts and the explainer, as routes that survive a reload. */

export type Route = 'california' | 'florida' | 'history' | 'how';

export const HASHES: Record<Route, string> = {
  california: '#/',
  florida: '#/florida',
  history: '#/history',
  how: '#/how-it-works',
};

export function routeFromHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, '').replace(/\/$/, '').toLowerCase();
  if (clean === 'florida') return 'florida';
  if (clean === 'history') return 'history';
  if (clean === 'how-it-works' || clean === 'how') return 'how';
  return 'california';
}

export function hashFor(route: Route): string {
  return HASHES[route];
}

/** Which state the map is showing. History and How it works keep the map's state. */
export function stateOf(route: Route): 'ca' | 'fl' {
  return route === 'florida' ? 'fl' : 'ca';
}

export function titleFor(route: Route): string {
  switch (route) {
    case 'florida':
      return 'Florida: Traffic Analyzer';
    case 'history':
      return 'History: Traffic Analyzer';
    case 'how':
      return 'How it works: Traffic Analyzer';
    default:
      return 'Traffic Analyzer';
  }
}
