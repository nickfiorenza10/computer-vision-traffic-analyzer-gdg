import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { ClassTable } from '../lib/aggregate';
import { CLASS_COLORS } from '../lib/aggregate';
import { DataTable } from './DataTable';

export type ClassChartProps = { table: ClassTable };

export function ClassChart({ table }: ClassChartProps) {
  const data = table.rows.map((row) => ({ label: row.label, ...row.counts, total: row.total }));
  const grandTotal = table.rows.reduce((sum, row) => sum + row.total, 0);

  return (
    <section className="card" aria-labelledby="class-heading">
      <header>
        <div>
          <h2 id="class-heading">Class breakdown</h2>
          <p className="note" style={{ margin: 0 }}>
            Vehicles per lane by class. A class with no vehicles in this window has no bar.
          </p>
        </div>
      </header>

      <div className="chart">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <CartesianGrid strokeDasharray="2 4" stroke="#1E2126" />
            <XAxis dataKey="label" tick={{ fontSize: 11 }} />
            <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={36} />
            <Tooltip contentStyle={{ borderRadius: 8, borderColor: '#dfe4df', fontSize: 12 }} />
            {/* Recharts sorts its own legend alphabetically; render the pipeline's class order instead. */}
            <Legend
              wrapperStyle={{ fontSize: 12 }}
              content={() => (
                <ul className="chart-legend">
                  {table.classes.map((name) => (
                    <li key={name}>
                      <span className="swatch" style={{ background: CLASS_COLORS[name] ?? '#8A9099' }} aria-hidden="true" />
                      {name}
                    </li>
                  ))}
                </ul>
              )}
            />
            {table.classes.map((name) => (
              <Bar
                key={name}
                dataKey={name}
                name={name}
                stackId="classes"
                fill={CLASS_COLORS[name] ?? '#8A9099'}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        label="Class breakdown, as a table"
        summary={`${grandTotal.toLocaleString()} vehicles, ${table.classes.length} classes present: ${table.classes.join(', ') || 'none'}.`}
        columns={['Lane', ...table.classes, 'Total']}
        rows={table.rows.map((row) => [
          row.label,
          ...table.classes.map((name) => row.counts[name] ?? 0),
          row.total,
        ])}
      />
    </section>
  );
}
