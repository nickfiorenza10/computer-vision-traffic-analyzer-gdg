import type { Summary } from '../lib/aggregate';

export function KpiTiles({ summary }: { summary: Summary }) {
  const { total, inbound, outbound, other, busiest } = summary;
  const split = total > 0 ? Math.round((inbound / total) * 100) : 0;

  return (
    <section className="kpis" aria-label="Key figures for the selected window">
      <div className="kpi">
        <div className="label">Vehicles counted</div>
        <div className="value">{total.toLocaleString()}</div>
        <div className="detail">crossings in this window</div>
      </div>
      <div className="kpi">
        <div className="label">Inbound vs outbound</div>
        <div className="value">
          {inbound.toLocaleString()} / {outbound.toLocaleString()}
        </div>
        <div className="detail">
          {total > 0 ? `${split}% inbound` : 'no events'}
          {other > 0 ? `, ${other.toLocaleString()} other` : ''}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Busiest lane</div>
        <div className="value">{busiest ? `Lane ${busiest.lane}` : '—'}</div>
        <div className="detail">
          {busiest ? `${busiest.count.toLocaleString()} vehicles` : 'no events in range'}
        </div>
      </div>
    </section>
  );
}
