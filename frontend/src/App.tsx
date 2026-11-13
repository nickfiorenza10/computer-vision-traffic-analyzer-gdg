import { useEffect, useState } from 'react';

import { SiteFooter, SiteHeader, useWorkerOnline } from './components/site';
import { titleFor, routeFromHash, type Route } from './lib/routes';
import { FloridaPage } from './views/FloridaPage';
import { HistoryView } from './views/HistoryView';
import { HowItWorks } from './views/HowItWorks';
import { MapPage } from './views/MapPage';

/** One shell: the map for California or Florida, the charts, and the explainer, all by hash. */
export default function App() {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash));
  const online = useWorkerOnline();

  useEffect(() => {
    const onHashChange = () => setRoute(routeFromHash(window.location.hash));
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    document.title = titleFor(route);
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior });
  }, [route]);

  return (
    <div className="site">
      <SiteHeader route={route} workerOnline={online !== false} />
      {route === 'california' ? <MapPage /> : null}
      {route === 'florida' ? <FloridaPage /> : null}
      {route === 'history' ? <HistoryView /> : null}
      {route === 'how' ? <HowItWorks /> : null}
      <SiteFooter />
    </div>
  );
}
