// Editor SQL con botón Ejecutar (Ctrl+Enter), Limpiar, consultas de
// ejemplo y dropdown de historial (últimas consultas exitosas).
const EXAMPLES = [
  {
    label: 'KNN: 5 restaurantes cerca de (-76.8, 39.2)',
    sql: `SELECT name, city FROM fast_food_restaurants_usa WHERE location KNN ((-76.8, 39.2), 5);`,
  },
  {
    label: 'Radio espacial: 0.5° alrededor de (-76.8, 39.2)',
    sql: `SELECT name, city FROM fast_food_restaurants_usa WHERE location IN ((-76.8, 39.2), 0.5);`,
  },
  {
    label: 'Rango con B+Tree sobre big_test',
    sql: `SELECT * FROM big_test WHERE id BETWEEN 1000 AND 1050;`,
  },
  {
    label: 'Búsqueda por show_id en netflix_titles',
    sql: `SELECT title, director, release_year FROM netflix_titles WHERE show_id = 's1';`,
  },
  {
    label: 'CREATE TABLE desde CSV',
    sql: `CREATE TABLE restaurantes FROM FILE "restaurantes.csv";`,
  },
]

function shortLabel(q) {
  const oneLine = q.replace(/\s+/g, ' ').trim()
  return oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine
}

export default function SqlEditor({ sql, setSql, onExecute, onClear, executing, history = [] }) {
  const handleKeyDown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      onExecute()
    }
  }

  const handleExample = (e) => {
    const idx = e.target.value
    if (idx !== '') {
      setSql(EXAMPLES[Number(idx)].sql)
      e.target.value = ''
    }
  }

  const handleHistory = (e) => {
    const idx = e.target.value
    if (idx !== '') {
      setSql(history[Number(idx)])
      e.target.value = ''
    }
  }

  return (
    <section className="rounded-card border border-hairline bg-surface p-6 shadow-card">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-[15px]">Consultas SQL</h2>
        <div className="flex items-center gap-2">
          <select
            defaultValue=""
            onChange={handleHistory}
            disabled={history.length === 0}
            title="Historial de consultas ejecutadas con éxito"
            className="max-w-36 rounded-input border border-hairline bg-surface px-2 py-1 text-xs text-body outline-none focus:border-border-strong disabled:opacity-50"
          >
            <option value="" disabled>
              Historial…
            </option>
            {history.map((q, i) => (
              <option key={i} value={i}>
                {shortLabel(q)}
              </option>
            ))}
          </select>
          <select
            defaultValue=""
            onChange={handleExample}
            className="rounded-input border border-hairline bg-surface px-2 py-1 text-xs text-body outline-none focus:border-border-strong"
          >
            <option value="" disabled>
              Ejemplos…
            </option>
            {EXAMPLES.map((ex, i) => (
              <option key={ex.label} value={i}>
                {ex.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <textarea
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        onKeyDown={handleKeyDown}
        rows={10}
        spellCheck={false}
        placeholder="Escribe tu consulta SQL aquí…  (Ctrl+Enter para ejecutar)"
        className="w-full resize-y rounded-input border border-hairline bg-canvas/50 p-4 font-mono text-[13px] leading-relaxed text-ink outline-none placeholder:text-helper focus:border-border-strong"
      />

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={onExecute}
          disabled={executing || !sql.trim()}
          className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-edge disabled:opacity-50"
        >
          {executing ? 'Ejecutando…' : 'Ejecutar'}
        </button>
        <button
          type="button"
          onClick={onClear}
          className="rounded-full border border-hairline px-5 py-2 text-sm text-ink transition-colors hover:bg-canvas"
        >
          Limpiar
        </button>
        <span className="ml-auto hidden text-xs text-helper sm:inline">
          Ctrl + Enter ejecuta la consulta
        </span>
      </div>
    </section>
  )
}
