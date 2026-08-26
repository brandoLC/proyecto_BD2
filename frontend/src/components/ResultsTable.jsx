// Pestaña Resultados: tabla de datos o mensaje de éxito para no-SELECT.
export default function ResultsTable({ result }) {
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

  return (
    <div>
      <p className="mb-3 font-mono text-xs text-helper">
        {result.rowcount ?? result.rows?.length ?? 0} filas ·{' '}
        {result.elapsed_ms?.toFixed?.(1) ?? result.elapsed_ms} ms
      </p>
      <div className="max-h-96 overflow-auto rounded-input border border-hairline">
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
            {(result.rows || []).map((row, i) => (
              <tr key={i} className={i % 2 === 1 ? 'bg-canvas' : 'bg-surface'}>
                {row.map((cell, j) => (
                  <td
                    key={j}
                    className="border-b border-hairline px-3 py-1.5 text-body last:border-b-0"
                  >
                    {formatCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
            {(result.rows || []).length === 0 && (
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
    </div>
  )
}

function formatCell(cell) {
  if (cell === null || cell === undefined) return 'NULL'
  if (typeof cell === 'object') return JSON.stringify(cell)
  return String(cell)
}
