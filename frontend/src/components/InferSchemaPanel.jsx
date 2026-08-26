// Tarjeta del asistente "Nuevo desde CSV": previsualiza el esquema inferido
// (columnas, filas de muestra, estimación) mientras el CREATE TABLE sugerido
// ya está cargado en el editor SQL.
const MAX_PREVIEW_ROWS = 5

function formatInt(n) {
  return typeof n === 'number' ? n.toLocaleString('es-PE') : '—'
}

export default function InferSchemaPanel({ infer, onClose }) {
  if (!infer) return null

  return (
    <div className="mb-4 rounded-input border border-hairline bg-surface p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-heading text-xs font-medium text-ink">
          {infer.loading ? 'Analizando CSV…' : `Nuevo desde CSV: ${infer.data?.table_name ?? ''}`}
        </h3>
        <button
          type="button"
          onClick={onClose}
          title="Cerrar"
          className="rounded-full border border-hairline px-2 py-0.5 text-xs leading-none text-helper transition-colors hover:bg-canvas hover:text-ink"
        >
          ✕
        </button>
      </div>

      {infer.loading && (
        <div className="mt-2 flex items-center gap-2 text-xs text-helper">
          <span className="h-3 w-3 animate-spin rounded-full border border-helper border-t-transparent" />
          Cargando…
        </div>
      )}

      {infer.error && (
        <div className="mt-2 flex items-start gap-2 rounded-input border border-error/40 bg-error/5 px-3 py-2">
          {infer.error.stage && (
            <span className="mt-0.5 shrink-0 rounded-full border border-error/60 px-2 py-0.5 font-mono text-[10px] text-error">
              {infer.error.stage}
            </span>
          )}
          <p className="font-mono text-[11px] leading-relaxed text-error">{infer.error.error}</p>
        </div>
      )}

      {infer.data && (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex flex-wrap gap-1">
            {(infer.data.columns || []).map((c) =>
              c.primary_key ? (
                <span
                  key={c.name}
                  className="rounded-full border border-accent px-2 py-0.5 font-mono text-[10px] text-accent-edge"
                >
                  {c.name} · {c.type} · PK
                </span>
              ) : (
                <span
                  key={c.name}
                  className="rounded-full border border-hairline px-2 py-0.5 font-mono text-[10px] text-body"
                >
                  {c.name} · {c.type}
                </span>
              ),
            )}
          </div>

          {(infer.data.preview_rows || []).length > 0 && (
            <div className="overflow-x-auto rounded-input border border-hairline">
              <table className="w-full border-collapse font-mono text-[11px]">
                <thead>
                  <tr className="border-b border-hairline bg-canvas/60">
                    {(infer.data.columns || []).map((c) => (
                      <th
                        key={c.name}
                        className="px-2 py-1 text-left font-medium whitespace-nowrap text-ink"
                      >
                        {c.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {infer.data.preview_rows.slice(0, MAX_PREVIEW_ROWS).map((row, i) => (
                    <tr key={i} className="border-b border-hairline last:border-b-0">
                      {row.map((cell, j) => (
                        <td key={j} className="px-2 py-1 whitespace-nowrap text-body">
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-[11px] text-helper">
            ≈ {formatInt(infer.data.total_rows_estimate)} filas estimadas
          </p>

          <p className="rounded-input border border-accent/40 bg-highlight/30 px-3 py-2 text-[11px] text-body">
            Revisa y ejecuta el CREATE TABLE sugerido, luego usa 'Cargar CSV' en la tabla
            creada.
          </p>
        </div>
      )}
    </div>
  )
}
