// Editor SQL con botón Ejecutar (Ctrl+Enter), Limpiar, consultas de
// ejemplo y dropdown de historial (últimas consultas exitosas).
// Resaltado de sintaxis: <pre> con spans coloreados (.tok-*) detrás del
// textarea (texto transparente, caret visible), scroll sincronizado.
import { useMemo, useRef } from 'react'

const KEYWORDS = new Set([
  'SELECT', 'FROM', 'WHERE', 'CREATE', 'TABLE', 'INDEX', 'USING', 'BTREE',
  'HASH', 'RTREE', 'HEAP', 'SEQUENTIAL', 'INSERT', 'INTO', 'VALUES',
  'DELETE', 'DROP', 'LOAD', 'FILE', 'KNN', 'IN', 'BETWEEN', 'AND', 'LIMIT',
  'OFFSET', 'COUNT', 'MIN', 'MAX', 'SUM', 'AVG', 'PRIMARY', 'KEY', 'INT',
  'FLOAT', 'VARCHAR', 'TEXT', 'BOOL', 'POINT', 'TRUE', 'FALSE', 'SERIAL',
])

// Tokenizer del dialecto MiniDB. Orden de alternancia: comentario,
// strings (simples y dobles, con escape por duplicado), números,
// palabras (keyword o identificador) y operadores/puntuación.
const TOKEN_RE =
  /(--[^\n]*)|('(?:[^']|'')*')|("(?:[^"]|"")*")|(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(<=|>=|<>|!=|=|<|>|\+|-|\*|\/|%|\(|\)|,|;|\.)/g

function highlightSql(sql) {
  const nodes = []
  let last = 0
  let key = 0
  TOKEN_RE.lastIndex = 0
  let m
  while ((m = TOKEN_RE.exec(sql))) {
    if (m.index > last) nodes.push(sql.slice(last, m.index))
    const [full, comment, sq, dq, num, word, op] = m
    if (comment) nodes.push(<span key={key++} className="tok-comment">{comment}</span>)
    else if (sq || dq) nodes.push(<span key={key++} className="tok-string">{sq || dq}</span>)
    else if (num) nodes.push(<span key={key++} className="tok-number">{num}</span>)
    else if (word) {
      if (KEYWORDS.has(word.toUpperCase())) {
        nodes.push(<span key={key++} className="tok-keyword">{word}</span>)
      } else {
        nodes.push(word)
      }
    } else if (op) nodes.push(<span key={key++} className="tok-operator">{op}</span>)
    last = m.index + full.length
  }
  if (last < sql.length) nodes.push(sql.slice(last))
  // Si el texto termina en salto de línea, el textarea muestra una línea
  // vacía final que el <pre> colapsa; el espacio extra mantiene la misma
  // altura para que el scroll no se desalinee.
  if (sql.endsWith('\n')) nodes.push(' ')
  return nodes
}
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
  {
    label: 'CREATE TABLE sequential',
    sql: `CREATE TABLE empleados (id INT PRIMARY KEY, nombre VARCHAR(30), salario FLOAT) USING SEQUENTIAL;`,
  },
  {
    label: 'Rango sobre tabla sequential',
    sql: `SELECT * FROM empleados WHERE id BETWEEN 10 AND 20;`,
  },
]

function shortLabel(q) {
  const oneLine = q.replace(/\s+/g, ' ').trim()
  return oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine
}

export default function SqlEditor({ sql, setSql, onExecute, onClear, executing, history = [] }) {
  const highlightRef = useRef(null)
  const highlighted = useMemo(() => highlightSql(sql), [sql])

  // El <pre> resaltado está detrás y no scrollea solo; se arrastra con el
  // scroll del textarea para que ambas capas queden alineadas.
  const syncScroll = (e) => {
    const pre = highlightRef.current
    if (!pre) return
    pre.scrollTop = e.target.scrollTop
    pre.scrollLeft = e.target.scrollLeft
  }

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

      <div className="relative rounded-input bg-canvas/50">
        {/* Capa de resaltado: mismo font/padding que el textarea, detrás
            (el textarea define el tamaño y conserva su resize-y). inset-px
            deja libre el borde de 1px del textarea. */}
        <pre
          ref={highlightRef}
          aria-hidden="true"
          className="pointer-events-none absolute inset-px overflow-hidden whitespace-pre-wrap break-words p-4 font-mono text-[13px] leading-relaxed text-ink"
        >
          {highlighted}
        </pre>
        <textarea
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={handleKeyDown}
          onScroll={syncScroll}
          rows={10}
          spellCheck={false}
          placeholder="Escribe tu consulta SQL aquí…  (Ctrl+Enter para ejecutar)"
          className="relative w-full resize-y rounded-input border border-hairline bg-transparent p-4 font-mono text-[13px] leading-relaxed text-transparent caret-ink outline-none placeholder:text-helper focus:border-border-strong"
        />
      </div>

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
