// Pestaña Resultados: tabla de datos o mensaje de éxito para no-SELECT.
// Incluye exportación a CSV y celdas truncadas con vista completa al clic.
import { useState } from 'react'

function formatCell(cell) {
  if (cell === null || cell === undefined) return 'NULL'
  if (typeof cell === 'object') return JSON.stringify(cell)
  return String(cell)
}

function csvEscape(value) {
  const s = formatCell(value)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function toCsv(columns, rows) {
  const lines = [columns.map(csvEscape).join(',')]
  for (const row of rows) lines.push(row.map(csvEscape).join(','))
  return lines.join('\n')
}

export default function ResultsTable({ result, tableName }) {
  const [cellView, setCellView] = useState(null) // {column, value} del modal

  if (!result) {
    return (
      <p className="py-10 text-center text-xs text-helper">
        Ejecuta una consulta para ver los resultados.
      </p>
    )
  }

  const isSelect = result.kind === 'select' && result.columns

  if (!isSelect) {
    return (
      <div className="py-6">
        <p className="text-sm text-success">✓ {result.message || 'OK'}</p>
        <p className="mt-1 font-mono text-xs text-helper">
          {result.elapsed_ms?.toFixed?.(1) ?? result.elapsed_ms} ms
          {result.rowcount != null && ` · ${result.rowcount} filas afectadas`}
        </p>
      </div>
    )
  }

  const rows = result.rows || []
  const rowcount = result.rowcount ?? rows.length

  const exportCsv = () => {
    const blob = new Blob([toCsv(result.columns, rows)], {
      type: 'text/csv;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${tableName || 'resultado'}_${rowcount}_filas.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="font-mono text-xs text-helper">
          {rowcount} filas · {result.elapsed_ms?.toFixed?.(1) ?? result.elapsed_ms} ms
        </p>
        <button
          type="button"
          onClick={exportCsv}
          disabled={rows.length === 0}
          title="Descargar el resultado como CSV"
          className="inline-flex items-center gap-1.5 rounded-full border border-hairline px-3 py-1 text-xs text-ink transition-colors hover:bg-canvas disabled:opacity-50"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-3.5 w-3.5"
            aria-hidden="true"
          >
            <path d="M12 4v12m0 0l-4-4m4 4l4-4" />
            <path d="M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2" />
          </svg>
          Exportar
        </button>
      </div>

      <div className="max-h-96 max-w-full overflow-auto rounded-input border border-hairline">
        <table className="w-full border-collapse font-mono text-[13px]">
          <thead className="sticky top-0 z-10">
            <tr className="bg-canvas">
              {result.columns.map((c) => (
                <th
                  key={c}
                  className="border-b border-hairline px-3 py-2 text-left font-medium text-ink"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className={i % 2 === 1 ? 'bg-canvas' : 'bg-surface'}>
                {row.map((cell, j) => {
                  const text = formatCell(cell)
                  return (
                    <td
                      key={j}
                      className="border-b border-hairline px-3 py-1.5 text-body last:border-b-0"
                    >
                      {text === '' ? (
                        ''
                      ) : (
                        <span
                          role="button"
                          tabIndex={0}
                          title={text}
                          onClick={() =>
                            setCellView({ column: result.columns[j], value: text })
                          }
                          onKeyDown={(e) => {
                            if (e.key === 'Enter')
                              setCellView({ column: result.columns[j], value: text })
                          }}
                          className="block max-w-[280px] cursor-pointer truncate hover:text-ink"
                        >
                          {text}
                        </span>
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={result.columns.length}
                  className="px-3 py-6 text-center text-xs text-helper"
                >
                  La consulta no devolvió filas.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {cellView && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setCellView(null)}
        >
          <div
            className="max-h-[70vh] w-full max-w-lg overflow-auto rounded-card border border-hairline bg-surface p-5 shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="font-mono text-xs text-helper">{cellView.column}</span>
              <button
                type="button"
                onClick={() => setCellView(null)}
                title="Cerrar"
                className="rounded-full px-1.5 text-helper transition-colors hover:text-ink"
              >
                ✕
              </button>
            </div>
            <p className="whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed text-ink">
              {cellView.value}
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
