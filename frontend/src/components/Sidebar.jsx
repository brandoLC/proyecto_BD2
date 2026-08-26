// Panel izquierdo "Archivos": tablas, columnas, índices y archivos físicos.
function formatBytes(n) {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const INDEX_BADGE = {
  BTREE: 'B-Tree',
  HASH: 'Hash',
  RTREE: 'R-Tree',
}

export default function Sidebar({ tables, loading, error, onRefresh, onSelectTable }) {
  return (
    <aside className="flex flex-col rounded-card border border-hairline bg-surface p-6 shadow-card">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-[15px]">Archivos</h2>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="rounded-full border border-hairline px-3 py-1 text-xs text-ink transition-colors hover:bg-canvas disabled:opacity-50"
        >
          {loading ? 'Cargando…' : 'Actualizar'}
        </button>
      </div>

      {error && (
        <p className="mb-3 rounded-input border border-error/40 bg-error/5 px-3 py-2 text-xs text-error">
          {error}
        </p>
      )}

      {!error && tables.length === 0 && !loading && (
        <p className="text-xs text-helper">
          No hay tablas todavía. Crea una con CREATE TABLE.
        </p>
      )}

      <div className="flex flex-col gap-3 overflow-y-auto">
        {tables.map((t) => (
          <div key={t.name} className="rounded-input border border-hairline p-3">
            <button
              type="button"
              onClick={() => onSelectTable(t.name)}
              className="flex w-full items-baseline justify-between gap-2 text-left"
              title={`Seleccionar de ${t.name}`}
            >
              <span className="font-heading text-sm font-medium text-ink hover:text-accent">
                {t.name}
              </span>
              <span className="font-mono text-[11px] text-helper">
                {t.rowcount} filas
              </span>
            </button>

            <div className="mt-2 flex flex-col gap-0.5">
              {(t.columns || []).map((c) => (
                <div key={c.name} className="flex items-baseline justify-between gap-2">
                  <span className="font-mono text-xs text-body">
                    {c.name}
                    {c.primary_key && (
                      <span className="ml-1 text-[10px] text-accent-edge">PK</span>
                    )}
                  </span>
                  <span className="font-mono text-[11px] text-helper">{c.type}</span>
                </div>
              ))}
            </div>

            {(t.indexes || []).length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {t.indexes.map((ix) => (
                  <span
                    key={ix.name}
                    title={`${ix.name} sobre ${ix.column}`}
                    className="rounded-full border border-accent px-2 py-0.5 font-mono text-[10px] text-accent-edge"
                  >
                    {INDEX_BADGE[ix.type] || ix.type} · {ix.column}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {tables.some((t) => (t.files || []).length > 0) && (
        <div className="mt-5 border-t border-hairline pt-4">
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-helper">
            Archivos
          </h3>
          <div className="flex flex-col gap-1.5">
            {tables.flatMap((t) =>
              (t.files || []).map((f) => (
                <div key={f.path} className="flex items-baseline justify-between gap-2">
                  <span className="truncate font-mono text-[11px] text-body" title={f.path}>
                    {f.path}
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-helper">
                    {formatBytes(f.size_bytes)} · {f.pages} págs
                  </span>
                </div>
              )),
            )}
          </div>
        </div>
      )}
    </aside>
  )
}
