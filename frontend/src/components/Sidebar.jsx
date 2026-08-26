// Panel izquierdo "Archivos": tablas, columnas, índices y archivos físicos.
// Incluye carga de CSV por tabla y el asistente "Nuevo desde CSV".
import { useRef, useState } from 'react'
import CsvUploadStatus from './CsvUploadStatus.jsx'
import InferSchemaPanel from './InferSchemaPanel.jsx'

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

export default function Sidebar({
  tables,
  loading,
  error,
  onRefresh,
  onSelectTable,
  csvUploads,
  onUploadCsv,
  infer,
  onInfer,
  onClearInfer,
}) {
  const uploadInputRef = useRef(null)
  const inferInputRef = useRef(null)
  const [uploadTarget, setUploadTarget] = useState(null) // tabla destino de la carga abierta

  const openUploadPicker = (name) => {
    setUploadTarget(name)
    uploadInputRef.current?.click()
  }

  const handleUploadFile = (e) => {
    const file = e.target.files?.[0]
    if (file && uploadTarget) onUploadCsv(uploadTarget, file)
    e.target.value = '' // permitir re-elegir el mismo archivo
  }

  const handleInferFile = (e) => {
    const file = e.target.files?.[0]
    if (file) onInfer(file)
    e.target.value = ''
  }

  return (
    <aside className="flex flex-col rounded-card border border-hairline bg-surface p-6 shadow-card">
      <input
        ref={uploadInputRef}
        type="file"
        accept=".csv"
        className="hidden"
        onChange={handleUploadFile}
      />
      <input
        ref={inferInputRef}
        type="file"
        accept=".csv"
        className="hidden"
        onChange={handleInferFile}
      />

      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="text-[15px]">Archivos</h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => inferInputRef.current?.click()}
            className="rounded-full border border-hairline px-3 py-1 text-xs text-ink transition-colors hover:bg-canvas"
          >
            Nuevo desde CSV
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="rounded-full border border-hairline px-3 py-1 text-xs text-ink transition-colors hover:bg-canvas disabled:opacity-50"
          >
            {loading ? 'Cargando…' : 'Actualizar'}
          </button>
        </div>
      </div>

      <InferSchemaPanel infer={infer} onClose={onClearInfer} />

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

            <div className="mt-2 flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => openUploadPicker(t.name)}
                disabled={csvUploads?.[t.name]?.loading}
                title={`Cargar un archivo CSV en ${t.name}`}
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
                  <path d="M12 16V4m0 0l-4 4m4-4l4 4" />
                  <path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" />
                </svg>
                {csvUploads?.[t.name]?.loading ? 'Cargando…' : 'Cargar CSV'}
              </button>
              {csvUploads?.[t.name]?.loading && (
                <span className="h-3 w-3 animate-spin rounded-full border border-helper border-t-transparent" />
              )}
            </div>

            <CsvUploadStatus status={csvUploads?.[t.name]} />
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
