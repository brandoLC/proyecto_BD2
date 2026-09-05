import { useCallback, useEffect, useState } from 'react'
import { getHealth, getTables, inferSchema, postQuery, uploadCsv } from './api.js'
import useTheme from './hooks/useTheme.js'
import TopNav from './components/TopNav.jsx'
import Sidebar from './components/Sidebar.jsx'
import SqlEditor from './components/SqlEditor.jsx'
import StatusMessage from './components/StatusMessage.jsx'
import ResultsTable from './components/ResultsTable.jsx'
import PlanPanel from './components/PlanPanel.jsx'
import MapPanel from './components/MapPanel.jsx'

const TABS = ['Resultados', 'Plan', 'Mapa']
const HISTORY_KEY = 'minidb:history'
const HISTORY_MAX = 20
const SIDEBAR_KEY = 'minidb:sidebar'

// Extrae la tabla afectada por una sentencia del subconjunto MiniDB.
function extractTableName(query) {
  const m = query.match(
    /^\s*create\s+index\s+\w+\s+on\s+(\w+)|^\s*(?:select\b[\s\S]*?\bfrom|insert\s+into|create\s+table|drop\s+table|load\s+into)\s+(\w+)/i,
  )
  return m ? m[1] || m[2] : null
}

function loadHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY))
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function loadSidebarOpen() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) !== '0'
  } catch {
    return true
  }
}

export default function App() {
  const [theme, toggleTheme] = useTheme()
  const [health, setHealth] = useState('checking')
  const [tables, setTables] = useState([])
  const [tablesLoading, setTablesLoading] = useState(false)
  const [tablesError, setTablesError] = useState(null)

  const [sql, setSql] = useState('')
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState(null) // última respuesta ok:true
  const [queryError, setQueryError] = useState(null) // {error, stage} de ok:false
  const [lastSql, setLastSql] = useState('') // SQL que produjo `result`
  const [lastTable, setLastTable] = useState(null) // tabla de la consulta que produjo `result`
  const [activeTable, setActiveTable] = useState(null) // breadcrumb + nodo activo del árbol
  const [history, setHistory] = useState(loadHistory) // últimas consultas exitosas
  const [sidebarOpen, setSidebarOpen] = useState(loadSidebarOpen)
  const [tab, setTab] = useState('Resultados')

  const toggleSidebar = useCallback(() => {
    setSidebarOpen((prev) => {
      localStorage.setItem(SIDEBAR_KEY, prev ? '0' : '1')
      return !prev
    })
  }, [])

  // CSV: resultado/estado de carga por tabla, y estado del asistente "Nuevo desde CSV".
  const [csvUploads, setCsvUploads] = useState({}) // { [tabla]: {loading?, result?, error?} }
  const [infer, setInfer] = useState(null) // null | {loading} | {data} | {error:{error,stage}}
  // Columna POINT derivada por tabla inferida: { [tabla]: {column, lat_col, lng_col} }
  const [derivedPoints, setDerivedPoints] = useState({})

  const checkHealth = useCallback(async () => {
    try {
      const data = await getHealth()
      setHealth(data.status === 'ok' ? 'ok' : 'down')
    } catch {
      setHealth('down')
    }
  }, [])

  const loadTables = useCallback(async () => {
    setTablesLoading(true)
    setTablesError(null)
    try {
      const data = await getTables()
      setTables(data.tables || [])
    } catch (e) {
      setTablesError(`No se pudieron cargar las tablas: ${e.message}`)
      setTables([])
    } finally {
      setTablesLoading(false)
    }
  }, [])

  useEffect(() => {
    checkHealth()
    loadTables()
    const interval = setInterval(checkHealth, 15000)
    return () => clearInterval(interval)
  }, [checkHealth, loadTables])

  const execute = useCallback(async () => {
    const query = sql.trim()
    if (!query || executing) return
    setExecuting(true)
    setQueryError(null)
    try {
      const data = await postQuery(query)
      if (data.ok) {
        setResult(data)
        setLastSql(query)
        const name = extractTableName(query)
        setLastTable(name)
        // Historial: sin duplicados consecutivos, tope HISTORY_MAX.
        setHistory((prev) => {
          const next = (prev[0] === query ? prev : [query, ...prev]).slice(0, HISTORY_MAX)
          localStorage.setItem(HISTORY_KEY, JSON.stringify(next))
          return next
        })
        // Breadcrumb: la tabla de la consulta pasa a ser la activa;
        // si se eliminó la tabla activa, se vuelve a `minidb`.
        if (data.kind === 'drop_table') {
          setActiveTable((prev) => (prev === data.table ? null : prev))
        } else if (name) {
          setActiveTable(name)
        }
        // Refrescar metadatos si el esquema pudo cambiar.
        if (['create_table', 'create_index', 'insert', 'delete', 'drop_table'].includes(data.kind)) {
          loadTables()
        }
        // Una tabla recreada/eliminada invalida el mensaje de carga CSV anterior.
        if ((data.kind === 'create_table' || data.kind === 'drop_table') && data.table) {
          setCsvUploads((prev) => {
            const next = { ...prev }
            delete next[data.table]
            return next
          })
        }
        // El mapeo de POINT derivado solo se invalida al ELIMINAR la tabla;
        // al crearla se necesita intacto para el "Cargar CSV" que sigue.
        if (data.kind === 'drop_table' && data.table) {
          setDerivedPoints((prev) => {
            if (!(data.table in prev)) return prev
            const next = { ...prev }
            delete next[data.table]
            return next
          })
        }
      } else {
        setQueryError({ error: data.error || 'Error desconocido', stage: data.stage })
      }
    } catch (e) {
      setQueryError({ error: `Error de red: ${e.message}`, stage: null })
    } finally {
      setExecuting(false)
    }
  }, [sql, executing, loadTables])

  const clearAll = useCallback(() => {
    setSql('')
    setResult(null)
    setQueryError(null)
    setLastSql('')
    setLastTable(null)
  }, [])

  const selectTable = useCallback(
    (name) => {
      setSql(`SELECT * FROM ${name} LIMIT 100;`)
      setQueryError(null)
      setActiveTable(name)
    },
    [],
  )

  const uploadCsvToTable = useCallback(
    async (name, file) => {
      if (!file) return
      setCsvUploads((prev) => ({ ...prev, [name]: { loading: true } }))
      try {
        // Si el esquema se infirió con columna POINT derivada, enviar el mapeo.
        const data = await uploadCsv(name, file, derivedPoints[name])
        if (data.ok) {
          setCsvUploads((prev) => ({ ...prev, [name]: { result: data } }))
          setDerivedPoints((prev) => {
            if (!(name in prev)) return prev
            const next = { ...prev }
            delete next[name]
            return next
          })
          loadTables() // cambia el rowcount
        } else {
          setCsvUploads((prev) => ({
            ...prev,
            [name]: { error: { error: data.error || 'Error desconocido', stage: data.stage } },
          }))
        }
      } catch (e) {
        setCsvUploads((prev) => ({
          ...prev,
          [name]: { error: { error: `Error de red: ${e.message}`, stage: null } },
        }))
      }
    },
    [loadTables, derivedPoints],
  )

  const inferFromCsv = useCallback(async (file) => {
    if (!file) return
    setInfer({ loading: true })
    try {
      const data = await inferSchema(file)
      if (data.ok) {
        setInfer({ data })
        setSql(data.suggested_sql) // cargar el CREATE TABLE sugerido en el editor
        setQueryError(null)
        // Recordar la columna POINT derivada para la carga CSV posterior.
        if (data.derived_point) {
          setDerivedPoints((prev) => ({ ...prev, [data.table_name]: data.derived_point }))
        }
      } else {
        setInfer({ error: { error: data.error || 'Error desconocido', stage: data.stage } })
      }
    } catch (e) {
      setInfer({ error: { error: `Error de red: ${e.message}`, stage: null } })
    }
  }, [])

  const dismissCsv = useCallback((name) => {
    setCsvUploads((prev) => {
      const next = { ...prev }
      delete next[name]
      return next
    })
  }, [])

  return (
    <div className="min-h-screen bg-canvas">
      <TopNav
        health={health}
        theme={theme}
        onToggleTheme={toggleTheme}
        activeTable={activeTable}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={toggleSidebar}
      />

      <main className="mx-auto max-w-[1200px] px-4 py-6 sm:px-6">
        <div
          className={`grid grid-cols-1 gap-5 transition-[grid-template-columns] duration-200 ${
            sidebarOpen ? 'lg:grid-cols-[300px_1fr]' : 'lg:grid-cols-[48px_1fr]'
          }`}
        >
          {sidebarOpen ? (
            <Sidebar
              tables={tables}
              loading={tablesLoading}
              error={tablesError}
              onRefresh={loadTables}
              onSelectTable={selectTable}
              activeTable={activeTable}
              csvUploads={csvUploads}
              onUploadCsv={uploadCsvToTable}
              onDismissCsv={dismissCsv}
              infer={infer}
              onInfer={inferFromCsv}
              onClearInfer={() => setInfer(null)}
            />
          ) : (
            // Riel colapsado: botón vertical para reabrir el panel.
            <aside className="flex rounded-card border border-hairline bg-surface shadow-card lg:w-12 lg:flex-col lg:items-center lg:py-3">
              <button
                type="button"
                onClick={toggleSidebar}
                title="Mostrar panel de archivos"
                aria-label="Mostrar panel de archivos"
                className="mx-auto rounded-full p-1.5 text-body transition-colors hover:bg-canvas hover:text-ink"
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4"
                  aria-hidden="true"
                >
                  <path d="M9 6l6 6-6 6" />
                </svg>
              </button>
            </aside>
          )}

          <div className="flex min-w-0 flex-col gap-5">
            <SqlEditor
              sql={sql}
              setSql={setSql}
              onExecute={execute}
              onClear={clearAll}
              executing={executing}
              history={history}
            />

            {queryError && (
              <StatusMessage error={queryError.error} stage={queryError.stage} />
            )}

            <section className="rounded-card border border-hairline bg-surface p-6 shadow-card">
              <div className="mb-4 flex gap-2">
                {TABS.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTab(t)}
                    className={
                      t === tab
                        ? 'rounded-full bg-dark px-4 py-1.5 text-sm text-white dark:text-canvas'
                        : 'rounded-full border border-hairline px-4 py-1.5 text-sm text-ink transition-colors hover:bg-canvas'
                    }
                  >
                    {t}
                  </button>
                ))}
              </div>

              {tab === 'Resultados' && <ResultsTable result={result} tableName={lastTable} />}
              {tab === 'Plan' && <PlanPanel result={result} />}
              {tab === 'Mapa' && <MapPanel result={result} sql={lastSql} theme={theme} />}
            </section>
          </div>
        </div>
      </main>
    </div>
  )
}
