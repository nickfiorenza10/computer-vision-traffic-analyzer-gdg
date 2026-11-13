import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, fetchCameras, fetchClasses, fetchLaneTotals, fetchTimeBins } from '../api';
import { binRows, classTable, laneBars, summarize } from '../lib/aggregate';
import { chooseBinSeconds, formatBinSeconds } from '../lib/bins';
import type { Camera, ClassCount, LaneTotal, TimeBin } from '../types';
import { ClassChart } from '../components/ClassChart';
import { HeaderControls } from '../components/HeaderControls';
import { KpiTiles } from '../components/KpiTiles';
import { LaneChart } from '../components/LaneChart';
import { ReplayPanel } from '../components/ReplayPanel';
import { DataState, Empty, ErrorState, Loading } from '../components/StateViews';
import { VolumeChart } from '../components/VolumeChart';

type DataBundle = { totals: LaneTotal[]; bins: TimeBin[]; classes: ClassCount[] };

const EMPTY: DataBundle = { totals: [], bins: [], classes: [] };

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}

/** The API window is half-open, so pad the end past the last event. */
function rangeEnd(iso: string | null): string {
  if (!iso) return '';
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  return new Date(ms + 1000).toISOString();
}

export function HistoryView() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [camerasLoading, setCamerasLoading] = useState(true);
  const [camerasError, setCamerasError] = useState<string | null>(null);

  const [cameraId, setCameraId] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [binOverride, setBinOverride] = useState<number | 'auto'>('auto');
  const [split, setSplit] = useState(false);

  const [data, setData] = useState<DataBundle>(EMPTY);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const loadCameras = useCallback(() => {
    const controller = new AbortController();
    setCamerasLoading(true);
    setCamerasError(null);
    fetchCameras(controller.signal)
      .then((response) => {
        setCameras(response.cameras);
        setCamerasLoading(false);
        const first = response.cameras[0];
        if (first) {
          setCameraId(first.camera_id);
          setStart(first.first_event ?? '');
          setEnd(rangeEnd(first.last_event));
        }
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setCamerasLoading(false);
        setCamerasError(messageOf(error));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => loadCameras(), [loadCameras]);

  const rangeError = useMemo(() => {
    if (!start || !end) return 'Choose a start and an end.';
    const a = Date.parse(start);
    const b = Date.parse(end);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return 'Those times could not be read.';
    if (a >= b) return 'The end must be after the start.';
    return null;
  }, [start, end]);

  const autoBin = chooseBinSeconds(start, end);
  const binSeconds = binOverride === 'auto' ? autoBin : binOverride;

  useEffect(() => {
    if (!cameraId || rangeError) {
      setData(EMPTY);
      setDataLoading(false);
      setDataError(null);
      return;
    }
    const controller = new AbortController();
    setDataLoading(true);
    setDataError(null);
    Promise.all([
      fetchLaneTotals(cameraId, start, end, controller.signal),
      fetchTimeBins(cameraId, start, end, binSeconds, controller.signal),
      fetchClasses(cameraId, start, end, controller.signal),
    ])
      .then(([totals, bins, classes]) => {
        setData({ totals: totals.totals, bins: bins.bins, classes: classes.classes });
        setDataLoading(false);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setDataLoading(false);
        setDataError(messageOf(error));
      });
    return () => controller.abort();
  }, [cameraId, start, end, binSeconds, rangeError, reloadKey]);

  const onCameraChange = (nextId: string) => {
    const camera = cameras.find((entry) => entry.camera_id === nextId);
    setCameraId(nextId);
    if (camera) {
      setStart(camera.first_event ?? '');
      setEnd(rangeEnd(camera.last_event));
    }
  };

  const summary = useMemo(() => summarize(data.totals), [data.totals]);
  const bars = useMemo(() => laneBars(data.totals), [data.totals]);
  const volume = useMemo(
    () => binRows(data.bins, start, end, binSeconds),
    [data.bins, start, end, binSeconds],
  );
  const classes = useMemo(() => classTable(data.classes), [data.classes]);

  const hasEvents = data.totals.length > 0;

  return (
    <section className="history">
      <div className="history-bar">
        <div className="sub">Vehicle counts by lane, direction, class and time</div>
        <button type="button" onClick={() => setReloadKey((value) => value + 1)} disabled={dataLoading}>
          {dataLoading ? 'Refreshing…' : 'Refresh data'}
        </button>
      </div>

      <main className="layout">
        {camerasLoading ? <Loading label="Loading cameras…" /> : null}
        {camerasError ? <ErrorState message={camerasError} onRetry={loadCameras} /> : null}

        {!camerasLoading && !camerasError ? (
          <>
            {cameras.length === 0 ? (
              <Empty
                title="No cameras yet."
                detail="Import a recording with database/load_events.py and it will appear here."
              />
            ) : (
              <HeaderControls
                cameras={cameras}
                cameraId={cameraId}
                onCameraChange={onCameraChange}
                start={start}
                end={end}
                onStartChange={setStart}
                onEndChange={setEnd}
                binSeconds={binSeconds}
                autoBin={binOverride === 'auto'}
                onBinChange={setBinOverride}
                rangeError={rangeError}
                busy={dataLoading}
              />
            )}

            <DataState
              loading={dataLoading && !hasEvents}
              error={dataError}
              onRetry={() => setReloadKey((value) => value + 1)}
              empty={!dataLoading && !dataError && !hasEvents && !!cameraId && !rangeError}
              emptyTitle="No events in this window."
              emptyDetail="The API returns empty arrays instead of zeros, so an empty window means no recorded data, not an empty road."
            >
              {hasEvents ? (
                <>
                  <KpiTiles summary={summary} />
                  <VolumeChart
                    rows={volume.rows}
                    laneKeys={volume.laneKeys}
                    laneDirectionKeys={volume.laneDirectionKeys}
                    splitByDirection={split}
                    onSplitChange={setSplit}
                    binLabel={formatBinSeconds(binSeconds)}
                  />
                  <div className="grid-2">
                    <LaneChart bars={bars} />
                    <ClassChart table={classes} />
                  </div>
                </>
              ) : null}
            </DataState>

            <ReplayPanel />
          </>
        ) : null}
      </main>
    </section>
  );
}