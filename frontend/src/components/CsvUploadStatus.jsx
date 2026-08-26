// Resultado de "Cargar CSV" para una tabla: éxito (verde) con errores
// rechazados expandibles y columnas ignoradas, o error de la API (ok:false).
function formatInt(n) {
  return typeof n === 'number' ? n.toLocaleString('es-PE') : '—'
}

export default function CsvUploadStatus({ status }) {
  if (!status) return null

  if (status.error) {
    return (
      <div className="mt-2 rounded-input border border-error/40 bg-error/5 px-3 py-2">
        <div className="flex items-start gap-2">
          {status.error.stage && (
            <span className="mt-0.5 shrink-0 rounded-full border border-error/60 px-2 py-0.5 font-mono text-[10px] text-error">
              {status.error.stage}
            </span>
          )}
          <p className="font-mono text-[11px] leading-relaxed text-error">{status.error.error}</p>
        </div>
      </div>
    )
  }

  const r = status.result
  if (!r) return null
  const rejected = r.rows_rejected > 0
  const ignored = (r.ignored_columns || []).length > 0

  return (
    <div className="mt-2 rounded-input border border-success/40 bg-success/5 px-3 py-2">
      <p className="text-xs text-success">
        {formatInt(r.rows_loaded)} filas cargadas en {Math.round(r.elapsed_ms ?? 0)} ms
      </p>

      {rejected && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-[11px] text-amber-700">
            {formatInt(r.rows_rejected)} filas rechazadas — ver errores
          </summary>
          <ul className="mt-1 flex flex-col gap-0.5">
            {(r.errors || []).map((e, i) => (
              <li key={i} className="font-mono text-[11px] leading-relaxed text-amber-800">
                línea {e.line}: {e.reason}
              </li>
            ))}
          </ul>
        </details>
      )}

      {ignored && (
        <p className="mt-1 text-[11px] text-helper">
          Columnas ignoradas:{' '}
          <span className="font-mono">{r.ignored_columns.join(', ')}</span>
        </p>
      )}
    </div>
  )
}
