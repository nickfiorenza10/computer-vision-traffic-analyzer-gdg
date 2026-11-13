import type { MapMode } from '../lib/map';
import { LEVEL_LABELS, UNSWEPT_COLOUR } from '../lib/map';
import { LEVEL_COLOURS } from '../lib/map';

type Props = {
  mode: MapMode;
  onModeChange: (mode: MapMode) => void;
  counts: Record<string, number>;
  total: number;
};

const ORDER = ['heavy', 'moderate', 'light', 'learning', 'night'] as const;

/** The toggle and the legend. A colour on the map means the same thing here. */
export function MapLegend({ mode, onModeChange, counts, total }: Props) {
  return (
    <div className="map-legend">
      <div className="segmented" role="group" aria-label="Heat layer">
        <button type="button" aria-pressed={mode === 'level'} onClick={() => onModeChange('level')}>
          Traffic level
        </button>
        <button type="button" aria-pressed={mode === 'coverage'} onClick={() => onModeChange('coverage')}>
          Camera coverage
        </button>
      </div>

      {mode === 'level' ? (
        <ul className="legend-list">
          {ORDER.map((level) => (
            <li key={level}>
              <span className="swatch" style={{ background: LEVEL_COLOURS[level] }} aria-hidden="true" />
              {LEVEL_LABELS[level]} <span className="sub">{counts[level] ?? 0}</span>
            </li>
          ))}
          <li>
            <span className="swatch" style={{ background: UNSWEPT_COLOUR }} aria-hidden="true" />
            Not swept yet <span className="sub">{counts.none ?? 0}</span>
          </li>
        </ul>
      ) : (
        <p className="note">
          {total.toLocaleString()} cameras, denser where the heat is. Every camera counts the same here.
        </p>
      )}

      <p className="note attribution">© OpenStreetMap contributors · OpenFreeMap · Caltrans</p>
    </div>
  );
}
