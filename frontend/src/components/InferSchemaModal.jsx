// Modal "Nuevo desde CSV": muestra el esquema inferido (nombre de tabla,
// filas, preview) y deja elegir la PRIMARY KEY antes de pre-llenar el
// editor SQL con el CREATE TABLE. Defensivo ante campos nuevos del
// backend: suggested_pk, row_count, preview, columns[].nulls/duplicates
// y table_exists tienen fallback si faltan.
import { useEffect, useMemo, useState } from 'react'

const NONE = '__none__'

function formatInt(n) {
  return typeof n === 'number' ? n.toLocaleString('es-PE') : null
}

// Normaliza la respuesta del backend (vieja o extendida) a la forma que
// usa el modal. Campos nuevos opcionales con fallback.
function normalize(data) {
  const table = data?.table_name ?? data?.table ?? ''
  const rowCount = data?.row_count ?? data?.total_rows_estimate ?? null
  const columns = Array.isArray(data?.columns) ? data.columns : []
  const tableExists = Boolean(data?.table_exists)

  // preview: [{col: valor,...}] (nuevo) o preview_rows: [[...]] (viejo).
  let previewRows = []
  if (Array.isArray(data?.preview) && data.preview.length > 0 && typeof data.preview[0] === 'object') {
    previewRows = data.preview.slice(0, 5)
  } else if (Array.isArray(data?.preview_rows)) {
    previewRows = data.preview_rows.slice(0, 5).map((row) =>
      Object.fromEntries(columns.map((c, i) => [c.name, row?.[i]])),
    )
  }

  // suggested_pk: nombre de columna recomendada o null.
  const suggestedPk = columns.some((c) => c.name === data?.suggested_pk)
    ? data.suggested_pk
    : null

  return { table, rowCount, columns, tableExists, previewRows, suggestedPk }
}

export default function InferSchemaModal({ data, onApply, onClose }) {
  const { table, rowCount, columns, tableExists, previewRows, suggestedPk } =
    useMemo(() => normalize(data), [data])

  const [selected, setSelected] = useState(suggestedPk ?? NONE)

  // suggested_pk puede llegar tarde (misma respuesta, pero por si acaso).
  useEffect(() => {
    setSelected(suggestedPk ?? NONE)
  }, [suggestedPk])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const selectedColumn =
    selected === NONE ? null : columns.find((c) => c.name === selected) || null
  const nulls = selectedColumn?.nulls ?? 0
  const duplicates = selectedColumn?.duplicates ?? 0
  const invalidPk = selectedColumn !== null && (nulls > 0 || duplicates > 0)

  const problems = []
  if (nulls > 0) problems.push(`${nulls} valores vacíos`)
  if (duplicates > 0) problems.push(`${duplicates} duplicados`)

  const rowsFmt = formatInt(rowCount)

  return (
    // Overlay oscurecido: clic fuera cierra, como Cancelar.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="infer-modal-title"
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-card border border-hairline bg-surface shadow-card"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-2 border-b border-hairline px-5 py-4">
          <h3 id="infer-modal-title" className="font-heading text-base font-medium text-ink">
            Nuevo desde CSV: <span className="font-mono">{table}</span>
          </h3>
          <button
            type="button"
            onClick={onClose}
            title="Cerrar"
            aria-label="Cerrar asistente"
            className="rounded-full border border-hairline px-2 py-0.5 text-xs leading-none text-helper transition-colors hover:bg-canvas hover:text-ink"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-4 overflow-y-auto px-5 py-4">
          {tableExists && (
            <p className="rounded-input border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-700 dark:text-amber-400">
              La tabla '{table}' ya existe. Crearla de nuevo dará error.
            </p>
          )}

          <p className="text-xs text-helper">
            {rowsFmt !== null
              ? `${rowsFmt} filas detectadas`
              : 'Filas detectadas: —'}
          </p>

          {previewRows.length > 0 && (
            <div>
              <h4 className="mb-1 text-[10px] font-medium uppercase tracking-wide text-helper">
                Vista previa (primeras {previewRows.length} filas)
              </h4>
              <div className="overflow-x-auto rounded-input border border-hairline">
                <table className="w-full border-collapse font-mono text-[11px]">
                  <thead>
                    <tr className="border-b border-hairline bg-canvas/60">
                      {columns.map((c) => (
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
                    {previewRows.map((row, i) => (
                      <tr key={i} className="border-b border-hairline last:border-b-0">
                        {columns.map((c) => (
                          <td key={c.name} className="px-2 py-1 whitespace-nowrap text-body">
                            {row[c.name] ?? ''}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <fieldset>
            <legend className="mb-1 text-[10px] font-medium uppercase tracking-wide text-helper">
              Primary Key
            </legend>
            <div className="flex flex-col gap-1.5">
              {columns.map((c) => {
                const cNulls = c.nulls ?? 0
                const cDups = c.duplicates ?? 0
                const bad = cNulls > 0 || cDups > 0
                return (
                  <label
                    key={c.name}
                    className={`flex items-center gap-2 rounded-input border px-3 py-1.5 text-xs transition-colors ${
                      selected === c.name
                        ? 'border-accent bg-highlight/30'
                        : 'border-hairline hover:bg-canvas'
                    } ${bad ? 'text-body' : 'text-ink'}`}
                  >
                    <input
                      type="radio"
                      name="infer-pk"
                      value={c.name}
                      checked={selected === c.name}
                      onChange={() => setSelected(c.name)}
                      className="accent-[#3ba6f1]"
                    />
                    <span className="font-mono">
                      {c.name} ({c.type})
                    </span>
                    {bad && (
                      <span className="ml-auto font-mono text-[10px] text-helper">
                        {cNulls > 0 && `${cNulls} vacíos`}
                        {cNulls > 0 && cDups > 0 && ' · '}
                        {cDups > 0 && `${cDups} duplicados`}
                      </span>
                    )}
                  </label>
                )
              })}
              <label
                className={`flex items-center gap-2 rounded-input border px-3 py-1.5 text-xs transition-colors ${
                  selected === NONE
                    ? 'border-accent bg-highlight/30'
                    : 'border-hairline hover:bg-canvas'
                }`}
              >
                <input
                  type="radio"
                  name="infer-pk"
                  value={NONE}
                  checked={selected === NONE}
                  onChange={() => setSelected(NONE)}
                  className="accent-[#3ba6f1]"
                />
                <span className="text-ink">Ninguna — asignar SERIAL automático</span>
              </label>
            </div>
          </fieldset>

          {invalidPk && (
            <p className="rounded-input border border-error/40 bg-error/5 px-3 py-2 text-xs text-error">
              Esta columna tiene {problems.join(' y ')} y no puede ser PRIMARY KEY.
            </p>
          )}

          <p className="rounded-input border border-accent/40 bg-highlight/30 px-3 py-2 text-[11px] text-body">
            Si no eliges ninguna, MiniDB asignará un id SERIAL como clave primaria
            oculta (como PostgreSQL e InnoDB).
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-hairline px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-full border border-hairline px-4 py-1.5 text-xs text-ink transition-colors hover:bg-canvas"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={() => onApply(selected === NONE ? null : selected)}
            disabled={invalidPk}
            className="rounded-full bg-dark px-4 py-1.5 text-xs text-white transition-colors hover:opacity-90 disabled:opacity-50 dark:text-canvas"
          >
            Aplicar
          </button>
        </div>
      </div>
    </div>
  )
}
