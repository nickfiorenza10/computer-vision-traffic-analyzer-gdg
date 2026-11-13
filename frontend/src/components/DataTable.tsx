/** Text alternative for a chart: a table toggle plus a one-line summary. */

import { useId, useState, type ReactNode } from 'react';

export type DataTableProps = {
  summary: string;
  columns: string[];
  rows: (string | number)[][];
  label: string;
};

export function DataTable({ summary, columns, rows, label }: DataTableProps) {
  const [open, setOpen] = useState(false);
  const id = useId();

  return (
    <div>
      <div className="toggle-row">
        <p className="note" style={{ margin: 0, flex: '1 1 16rem' }}>
          {summary}
        </p>
        <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((value) => !value)}>
          {open ? 'Hide table' : 'Show table'}
        </button>
      </div>
      {open ? (
        <div className="table-wrap" id={id}>
          <table>
            <caption className="sr-only">{label}</caption>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell as ReactNode}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
