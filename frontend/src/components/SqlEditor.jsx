// Editor SQL con botón Ejecutar (Ctrl+Enter), Limpiar y consultas de ejemplo.
const EXAMPLES = [
  {
    label: 'CREATE TABLE con POINT',
    sql: `CREATE TABLE restaurantes (\n  id INT PRIMARY KEY,\n  nombre VARCHAR(50),\n  rating FLOAT,\n  location POINT\n);`,
  },
  {
    label: 'CREATE INDEX BTREE (la PK ya trae uno)',
    sql: `CREATE INDEX idx_rating ON restaurantes (rating) USING BTREE;`,
  },
  {
    label: 'CREATE INDEX HASH',
    sql: `CREATE INDEX idx_nombre ON restaurantes (nombre) USING HASH;`,
  },
  {
    label: 'CREATE INDEX RTREE',
    sql: `CREATE INDEX idx_location ON restaurantes (location) USING RTREE;`,
  },
  {
    label: 'INSERT',
    sql: `INSERT INTO restaurantes VALUES (1, 'Punto Azul', 4.5, (-12.05, -77.04));`,
  },
  {
    label: 'SELECT por clave primaria',
    sql: `SELECT * FROM restaurantes WHERE id = 1;`,
  },
  {
    label: 'SELECT rango BETWEEN',
    sql: `SELECT * FROM restaurantes WHERE rating BETWEEN 4.0 AND 4.8;`,
  },
  {
    label: 'Búsqueda espacial por radio',
    sql: `SELECT * FROM restaurantes WHERE location IN ((-12.05, -77.04), 0.02);`,
  },
  {
    label: 'Búsqueda espacial KNN',
    sql: `SELECT * FROM restaurantes WHERE location KNN ((-12.05, -77.04), 5);`,
  },
  {
    label: 'DELETE',
    sql: `DELETE FROM restaurantes WHERE id = 1;`,
  },
]

export default function SqlEditor({ sql, setSql, onExecute, onClear, executing }) {
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

  return (
    <section className="rounded-card border border-hairline bg-surface p-6 shadow-card">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[15px]">Consultas SQL</h2>
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
