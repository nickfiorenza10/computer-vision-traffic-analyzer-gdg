import { Bar, BarChart, CartesianGrid, Cell, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { LaneBar } from '../lib/aggregate';
import { laneColor } from '../lib/aggregate';
import { DataTable } from './DataTable';

export type LaneChartProps = { bars: LaneBar[] };

/** Inbound is solid, outbound is hatched in the same lane colour, so direction
 *  never relies on colour alone. */
export function LaneChart({ bars }: LaneChartProps) {
  const busiest = bars.reduce<LaneBar | null>((best, bar) => (!best || bar.total > best.total ? bar : best), null);

  return (
    <section className="card" aria-labelledby="lane-heading">
      <header>
        <div>
          <h2 id="lane-heading">Lane comparison</h2>
          <p className="note" style={{ margin: 0 }}>
            Solid = inbound, hatched = outbound. Bars are labelled with the lane number.
          </p>
        </div>
      </header>

      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={bars} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <defs>
              {bars.map((bar) => (
                <pattern
                  key={bar.lane}
                  id={`outbound-hatch-${bar.lane}`}
                  patternUnits="userSpaceOnUse"
                  width="6"
                  height="6"
                  patternTransform="rotate(45)"
                >
                  <rect width="6" height="6" fill="#0E1013" />
                  <line x1="0" y1="0" x2="0" y2="6" stroke={laneColor(bar.lane)} strokeWidth="3" />
                </pattern>
              ))}
            </defs>
            <CartesianGrid strokeDasharray="2 4" stroke="#1E2126" />
            <XAxis dataKey="label" tick={{ fontSize: 11 }} />
            <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={36} />
            <Tooltip contentStyle={{ borderRadius: 8, borderColor: '#dfe4df', fontSize: 12 }} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="inbound" name="Inbound (solid)" fill="#14534b" isAnimationActive={false}>
              {bars.map((bar) => (
                <Cell key={`in-${bar.lane}`} fill={laneColor(bar.lane)} />
              ))}
            </Bar>
            <Bar dataKey="outbound" name="Outbound (hatched)" fill="#14534b" isAnimationActive={false}>
              {bars.map((bar) => (
                <Cell key={`out-${bar.lane}`} fill={`url(#outbound-hatch-${bar.lane})`} stroke={laneColor(bar.lane)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        label="Lane comparison, as a table"
        summary={
          busiest
            ? `Busiest lane: ${busiest.label} with ${busiest.total.toLocaleString()} vehicles. ${bars.length} lanes have events in this window.`
            : 'No lanes have events in this window.'
        }
        columns={['Lane', 'Inbound', 'Outbound', 'Other', 'Total']}
        rows={bars.map((bar) => [bar.label, bar.inbound, bar.outbound, bar.other, bar.total])}
      />
    </section>
  );
}
