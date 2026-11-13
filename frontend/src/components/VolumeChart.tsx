import { Bar, BarChart, CartesianGrid, Cell, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { BinRow } from '../lib/aggregate';
import { laneColor } from '../lib/aggregate';
import { DataTable } from './DataTable';

export type VolumeChartProps = {
  rows: BinRow[];
  laneKeys: string[];
  laneDirectionKeys: string[];
  splitByDirection: boolean;
  onSplitChange: (value: boolean) => void;
  binLabel: string;
};

function laneOfKey(key: string): number {
  return Number(key.replace('lane ', '').split(' ')[0]);
}

function slug(key: string): string {
  return key.replace(/[^a-z0-9]+/gi, '-');
}

export function VolumeChart(props: VolumeChartProps) {
  const { rows, laneKeys, laneDirectionKeys, splitByDirection, onSplitChange, binLabel } = props;
  const keys = splitByDirection ? laneDirectionKeys : laneKeys;

  const valueOf = (row: BinRow, key: string): number | null =>
    typeof row[key] === 'number' ? (row[key] as number) : null;
  const rowTotal = (row: BinRow) => keys.reduce((sum, key) => sum + (valueOf(row, key) ?? 0), 0);

  // Bars, not curves: a bin is one bar, so nothing is implied between bins and a
  // bin with no events is visibly absent. Partial bins cover less than a full bin
  // of footage, so they are hatched and left out of the peak.
  const complete = rows.filter((row) => !row.partial);
  const peak = complete.reduce<{ count: number; label: string } | null>((best, row) => {
    const total = rowTotal(row);
    return !best || total > best.count ? { count: total, label: row.label } : best;
  }, null);
  const partialCount = rows.length - complete.length;
  const grandTotal = rows.reduce((sum, row) => sum + rowTotal(row), 0);

  return (
    <section className="card" aria-labelledby="volume-heading">
      <header>
        <div>
          <h2 id="volume-heading">Volume over time</h2>
          <p className="note" style={{ margin: 0 }}>
            One bar per {binLabel} bin, stacked by {splitByDirection ? 'lane and direction' : 'lane'}. Hatched bars
            are partial: they start before the window or run past its end, so they cover less than {binLabel} of
            footage and are not counted in the peak.
          </p>
        </div>
        <div className="toggle-row" role="group" aria-label="Volume chart grouping">
          <button type="button" aria-pressed={!splitByDirection} onClick={() => onSplitChange(false)}>
            By lane
          </button>
          <button type="button" aria-pressed={splitByDirection} onClick={() => onSplitChange(true)}>
            Split by direction
          </button>
        </div>
      </header>

      <div className="chart chart-tall">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: 0 }} barCategoryGap="12%">
            <defs>
              {keys.map((key) => (
                <pattern
                  key={key}
                  id={`partial-${slug(key)}`}
                  patternUnits="userSpaceOnUse"
                  width="5"
                  height="5"
                  patternTransform="rotate(45)"
                >
                  <rect width="5" height="5" fill="#0E1013" />
                  <line x1="0" y1="0" x2="0" y2="5" stroke={laneColor(laneOfKey(key))} strokeWidth="2.5" />
                </pattern>
              ))}
            </defs>
            <CartesianGrid strokeDasharray="2 4" stroke="#1E2126" />
            <XAxis dataKey="label" minTickGap={24} tick={{ fontSize: 11 }} />
            <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={36} />
            <Tooltip
              contentStyle={{ borderRadius: 8, borderColor: '#dfe4df', fontSize: 12 }}
              labelFormatter={(label, payload) => {
                const row = payload?.[0]?.payload as BinRow | undefined;
                return row?.partial ? `${label} · partial bin` : String(label);
              }}
            />
            {/* Keep the series order of the data instead of Recharts' alphabetical sort. */}
            <Legend
              wrapperStyle={{ fontSize: 12 }}
              content={() => (
                <ul className="chart-legend">
                  {keys.map((key) => (
                    <li key={key}>
                      <span className="swatch" style={{ background: laneColor(laneOfKey(key)) }} aria-hidden="true" />
                      {key}
                    </li>
                  ))}
                </ul>
              )}
            />
            {keys.map((key) => (
              <Bar
                key={key}
                dataKey={key}
                name={key}
                stackId="all"
                fill={laneColor(laneOfKey(key))}
                stroke={splitByDirection && key.includes('out') ? '#0B0C0E' : undefined}
                strokeDasharray={splitByDirection && key.includes('out') ? '3 2' : undefined}
                isAnimationActive={false}
              >
                {rows.map((row, index) => (
                  <Cell
                    key={index}
                    fill={row.partial ? `url(#partial-${slug(key)})` : laneColor(laneOfKey(key))}
                  />
                ))}
              </Bar>
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        label="Volume over time, as a table"
        summary={
          peak
            ? `Peak ${peak.count.toLocaleString()} vehicles in the ${peak.label} bin, complete bins only${
                partialCount ? `; ${partialCount} partial bin${partialCount === 1 ? '' : 's'} at the edges are marked` : ''
              }. Total ${grandTotal.toLocaleString()} across ${rows.length} bins.`
            : `No complete bins in this window${partialCount ? ` (${partialCount} partial)` : ''}. Total ${grandTotal.toLocaleString()}.`
        }
        columns={['Bin start (UTC)', ...keys, 'All groups', 'Window']}
        rows={rows.map((row) => [
          row.label,
          ...keys.map((key) => valueOf(row, key) ?? 'no data'),
          rowTotal(row),
          row.partial ? 'partial' : 'full',
        ])}
      />
    </section>
  );
}
