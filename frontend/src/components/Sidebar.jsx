// Panel izquierdo "Archivos": árbol colapsable de tablas con columnas,
// índices y archivos físicos. La carga de CSV vive dentro de cada nodo
// expandido; el asistente "Nuevo desde CSV" queda en la cabecera.
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

const INDEX_DOT = {
  BTREE: 'bg-accent',
  HASH: 'bg-violet-500',
  RTREE: 'bg-teal-500',
}

function TableNode({
  table,
  active,
  open,
  onToggle,
  onSelectTable,
  uploadStatus,
  onPickCsv,
  onDismissCsv,
}) {
  // column -> tipo de índice, para el punto de color junto a cada columna
  const indexedColumns = {}
  for (const ix of table.indexes || []) indexedColumns[ix.column] = ix.type

  // Archivos físicos de ESTA tabla: <tabla>.heap, <tabla>_<col>.btree, ...
  const files = (table.files || []).filter((f) => {
    const base = f.path.split('/').pop()
    return base.startsWith(`${table.name}.`) || base.startsWith(`${table.name}_`)
  })

  return (
    <div
      className={`rounded-input border ${
        active ? 'border-accent' : 'border-hairline'
      }`}
    >
      <div className="flex items-center gap-1 px-2 py-1.5">
        <button
          type="button"
          onClick={onToggle}
          title={open ? 'Colapsar' : 'Expandir'}
          aria-label={open ? `Colapsar ${table.name}` : `Expandir ${table.name}`}
          aria-expanded={open}
          className="rounded p-0.5 text-helper transition-colors hover:text-ink"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
            aria-hidden="true"
          >
            <path d="M9 6l6 6-6 6" />
          </svg>
        </button>
        <button
          type="button"
          onClick={() => onSelectTable(table.name)}
          className="flex min-w-0 flex-1 items-baseline justify-between gap-2 text-left"
          title={`Seleccionar de ${table.name}`}
        >
          <span
            className={`truncate font-heading text-sm font-medium hover:text-accent ${
              active ? 'text-accent-edge' : 'text-ink'
            }`}
          >
            {table.name}
          </span>
          <span className="shrink-0 font-mono text-[11px] text-helper">
            {table.rowcount} filas
          </span>
        </button>
      </div>

      {open && (
        <div className="border-t border-hairline px-3 py-2">
          <h4 className="mb-1 text-[10px] font-medium uppercase tracking-wide text-helper">
            Columnas
          </h4>
          <div className="flex flex-col gap-0.5">
            {(table.columns || []).map((c) => (
              <div key={c.name} className="flex items-baseline justify-between gap-2">
                <span className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-body">
                  {indexedColumns[c.name] && (
                    <span
                      className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                        INDEX_DOT[indexedColumns[c.name]] || 'bg-helper'
                      }`}
                      title={`Índice ${INDEX_BADGE[indexedColumns[c.name]] || indexedColumns[c.name]}`}
                    />
                  )}
                  <span className="truncate">{c.name}</span>
                  {c.primary_key && (
                    <span className="shrink-0 rounded-full border border-accent px-1 font-mono text-[9px] leading-tight text-accent-edge">
                      PK
                    </span>
                  )}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-helper">{c.type}</span>
              </div>
            ))}
          </div>

          {(table.indexes || []).length > 0 && (
            <>
              <h4 className="mb-1 mt-3 text-[10px] font-medium uppercase tracking-wide text-helper">
                Índices
              </h4>
              <div className="flex flex-col gap-0.5">
                {table.indexes.map((ix) => (
                  <div key={ix.name} className="flex items-baseline justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-body">
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                          INDEX_DOT[ix.type] || 'bg-helper'
                        }`}
                      />
                      <span className="truncate" title={ix.name}>
                        {ix.name}
                      </span>
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-helper">
                      {INDEX_BADGE[ix.type] || ix.type} · {ix.column}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}

          {files.length > 0 && (
            <>
              <h4 className="mb-1 mt-3 text-[10px] font-medium uppercase tracking-wide text-helper">
                Archivos
              </h4>
              <div className="flex flex-col gap-0.5">
                {files.map((f) => (
                  <div key={f.path} className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-[11px] text-body" title={f.path}>
                      {f.path}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-helper">
                      {formatBytes(f.size_bytes)} · {f.pages} págs
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="mt-3 flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={() => onPickCsv(table.name)}
              disabled={uploadStatus?.loading}
              title={`Cargar un archivo CSV en ${table.name}`}
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
              {uploadStatus?.loading ? 'Cargando…' : 'Cargar CSV'}
            </button>
            {uploadStatus?.loading && (
              <span className="h-3 w-3 animate-spin rounded-full border border-helper border-t-transparent" />
            )}
          </div>

          <CsvUploadStatus
            status={uploadStatus}
            onDismiss={onDismissCsv}
          />
        </div>
      )}
    </div>
  )
}

export default function Sidebar({
  tables,
  loading,
  error,
  onRefresh,
  onSelectTable,
  activeTable,
  csvUploads,
  onUploadCsv,
  onDismissCsv,
  infer,
  onInfer,
  onClearInfer,
}) {
  const uploadInputRef = useRef(null)
  const inferInputRef = useRef(null)
  const [uploadTarget, setUploadTarget] = useState(null) // tabla destino de la carga abierta
  const [expanded, setExpanded] = useState({}) // { [tabla]: true } — colapsadas por defecto

  const toggleNode = (name) => {
    setExpanded((prev) => ({ ...prev, [name]: !prev[name] }))
  }

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

      <div className="flex flex-col gap-2 overflow-y-auto">
        {tables.map((t) => (
          <TableNode
            key={t.name}
            table={t}
            active={t.name === activeTable}
            open={!!expanded[t.name]}
            onToggle={() => toggleNode(t.name)}
            onSelectTable={onSelectTable}
            uploadStatus={csvUploads?.[t.name]}
            onPickCsv={openUploadPicker}
            onDismissCsv={onDismissCsv ? () => onDismissCsv(t.name) : undefined}
          />
        ))}
      </div>
    </aside>
  )
}
