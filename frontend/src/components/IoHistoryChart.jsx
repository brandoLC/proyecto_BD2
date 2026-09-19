// Pestaña Métricas: visor gráfico de métricas I/O. Barras horizontales
// con las últimas consultas exitosas, etiquetadas por el tipo de acceso
// del plan, más un resumen agregado (promedio de reads por tipo).
// Escala logarítmica para que una búsqueda indexada (~5 reads) siga
// siendo visible junto a un full scan (~1000+ reads).

// Mapea el nombre del paso de acceso del plan a una etiqueta corta.
// El orden importa: "Index Range Scan" matchea antes que "Index Scan".
const ACCESS_LABELS = [
  [/R-Tree KNN/i, 'KNN (R-Tree)'],
  [/R-Tree Radius/i, 'Radio (R-Tree)'],
  [/Binary Search/i, 'Binary Search'],
  [/Index Range Scan/i, 'Index Range Scan'],
  [/Index Scan/i, 'Index Scan'],
  [/Index Lookup/i, 'Index Lookup'],
  [/Sequential Scan/i, 'Sequential Scan'],
  [/Heap Insert/i, 'Insert'],
  [/Heap Delete/i, 'Delete'],
  [/Bulk Load/i, 'Bulk Load'],
]

// Extrae la etiqueta del primer paso de acceso del plan; si no hay
// ninguno reconocible, cae al kind de la consulta.
export function extractAccessType(plan, kind) {
  for (const step of plan || []) {
    for (const [re, label] of ACCESS_LABELS) {
      if (re.test(step.name || '')) return label
    }
  }
  return kind || 'Consulta'
}

function barColor(access) {
  if (/sequential/i.test(access)) return 'bg-orange-400'
  if (/knn|radio|r-tree/i.test(access)) return 'bg-violet-400'
  return 'bg-accent'
}

function shortSql(sql) {
  const oneLine = sql.replace(/\s+/g, ' ').trim()
  return oneLine.length > 80 ? `${oneLine.slice(0, 77)}…` : oneLine
}

// Promedio de reads por tipo de acceso, en el orden de primera aparición
// en el historial: [{access, avg, n}].
function aggregateByAccess(entries) {
  const groups = new Map()
  for (const e of entries) {
    const g = groups.get(e.access) || { access: e.access, total: 0, n: 0 }
    g.total += e.reads
    g.n += 1
    groups.set(e.access, g)
  }
  return [...groups.values()].map((g) => ({
    access: g.access,
    avg: g.total / g.n,
    n: g.n,
  }))
}

export default function IoHistoryChart({ entries = [] }) {
  if (entries.length === 0) return null

  const maxReads = Math.max(...entries.map((e) => e.reads), 1)
  const summary = aggregateByAccess(entries)

  return (
    <div>
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium text-ink">Historial de I/O</h3>
        <span className="font-mono text-[11px] text-helper">
          bloques de 4 KB leídos · escala logarítmica
        </span>
      </div>
      <div className="flex flex-col gap-1.5">
        {entries.map((e, i) => {
          // log1p comprime el rango; mínimo 3% para que reads=0/1 se vea.
          const pct = Math.max((Math.log1p(e.reads) / Math.log1p(maxReads)) * 100, 3)
          return (
            <div
              key={i}
              className="flex items-center gap-2"
              title={`${e.sql}\n${e.reads} lecturas · ${e.writes} escrituras`}
            >
              <span className="w-32 shrink-0 truncate font-mono text-[11px] text-body">
                {e.access}
              </span>
              <div className="h-3 min-w-0 flex-1 overflow-hidden rounded-full bg-canvas">
                <div
                  className={`h-full rounded-full ${barColor(e.access)}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="w-20 shrink-0 text-right font-mono text-[11px] text-helper">
                {e.reads} r · {e.writes} w
              </span>
            </div>
          )
        })}
      </div>
      <p className="mt-2 truncate font-mono text-[11px] text-helper">
        Última: {shortSql(entries[0].sql)}
      </p>

      {/* Resumen agregado: promedio de lecturas por tipo de acceso. */}
      <div className="mt-5 border-t border-hairline pt-4">
        <h3 className="mb-2 text-sm font-medium text-ink">Promedio de lecturas por acceso</h3>
        <div className="flex flex-col gap-1">
          {summary.map((s) => (
            <div key={s.access} className="flex items-center gap-2">
              <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${barColor(s.access)}`} />
              <span className="w-32 shrink-0 truncate font-mono text-[11px] text-body">
                {s.access}
              </span>
              <span className="font-mono text-[11px] text-ink">
                {s.avg.toFixed(1)} reads
              </span>
              <span className="font-mono text-[11px] text-helper">
                · {s.n} consulta{s.n === 1 ? '' : 's'}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
