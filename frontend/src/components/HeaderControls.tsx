import type { Camera } from '../types';
import { BIN_LADDER, formatBinSeconds } from '../lib/bins';

export type HeaderControlsProps = {
  cameras: Camera[];
  cameraId: string;
  onCameraChange: (cameraId: string) => void;
  start: string;
  end: string;
  onStartChange: (value: string) => void;
  onEndChange: (value: string) => void;
  binSeconds: number;
  autoBin: boolean;
  onBinChange: (value: number | 'auto') => void;
  rangeError: string | null;
  busy: boolean;
};

/** `datetime-local` wants "YYYY-MM-DDTHH:MM:SS"; the API wants RFC 3339 UTC. */
export function toLocalInput(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return '';
  return new Date(ms).toISOString().slice(0, 19);
}

/** datetime-local may omit seconds ("2026-10-09T08:00"); the API needs them. */
export function fromLocalInput(value: string): string {
  if (!value) return '';
  const withSeconds = value.length === 16 ? `${value}:00` : value;
  return `${withSeconds}Z`;
}

export function HeaderControls(props: HeaderControlsProps) {
  const {
    cameras,
    cameraId,
    onCameraChange,
    start,
    end,
    onStartChange,
    onEndChange,
    binSeconds,
    autoBin,
    onBinChange,
    rangeError,
    busy,
  } = props;

  return (
    <form className="controls" onSubmit={(event) => event.preventDefault()} aria-label="Camera and time range">
      <div className="field">
        <label htmlFor="camera">Camera</label>
        <select
          id="camera"
          value={cameraId}
          onChange={(event) => onCameraChange(event.target.value)}
          disabled={!cameras.length}
        >
          {cameras.length === 0 ? <option value="">No cameras</option> : null}
          {cameras.map((camera) => (
            <option key={camera.camera_id} value={camera.camera_id}>
              {camera.camera_id} ({camera.events.toLocaleString()} events)
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="start">Start (UTC)</label>
        <input
          id="start"
          type="datetime-local"
          step={1}
          value={toLocalInput(start)}
          onChange={(event) => onStartChange(fromLocalInput(event.target.value))}
          aria-invalid={rangeError ? true : undefined}
        />
      </div>

      <div className="field">
        <label htmlFor="end">End (UTC)</label>
        <input
          id="end"
          type="datetime-local"
          step={1}
          value={toLocalInput(end)}
          onChange={(event) => onEndChange(fromLocalInput(event.target.value))}
          aria-invalid={rangeError ? true : undefined}
        />
      </div>

      <div className="field">
        <label htmlFor="bin">Bin size</label>
        <select
          id="bin"
          value={autoBin ? 'auto' : String(binSeconds)}
          onChange={(event) => onBinChange(event.target.value === 'auto' ? 'auto' : Number(event.target.value))}
        >
          <option value="auto">Auto ({formatBinSeconds(binSeconds)})</option>
          {BIN_LADDER.map((size) => (
            <option key={size} value={size}>
              {formatBinSeconds(size)}
            </option>
          ))}
        </select>
      </div>

      <p className="hint" role={rangeError ? 'alert' : undefined}>
        {rangeError
          ? rangeError
          : busy
            ? 'Loading the selected window…'
            : 'Times are UTC and the window is half-open: start ≤ t < end. Gaps mean no data, not zero traffic.'}
      </p>
    </form>
  );
}
