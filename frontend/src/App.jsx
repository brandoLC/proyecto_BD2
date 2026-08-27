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
  const [tab, setTab] = useState('Resultados')

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
  }, [])

  const selectTable = useCallback(
    (name) => {
      setSql(`SELECT * FROM ${name} LIMIT 100;`)
      setQueryError(null)
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
      <TopNav health={health} theme={theme} onToggleTheme={toggleTheme} />

      <main className="mx-auto max-w-[1200px] px-4 py-6 sm:px-6">
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[300px_1fr]">
          <Sidebar
            tables={tables}
            loading={tablesLoading}
            error={tablesError}
            onRefresh={loadTables}
            onSelectTable={selectTable}
            csvUploads={csvUploads}
            onUploadCsv={uploadCsvToTable}
            onDismissCsv={dismissCsv}
            infer={infer}
            onInfer={inferFromCsv}
            onClearInfer={() => setInfer(null)}
          />

          <div className="flex flex-col gap-5">
            <SqlEditor
              sql={sql}
              setSql={setSql}
              onExecute={execute}
              onClear={clearAll}
              executing={executing}
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

              {tab === 'Resultados' && <ResultsTable result={result} />}
              {tab === 'Plan' && <PlanPanel result={result} />}
              {tab === 'Mapa' && <MapPanel result={result} sql={lastSql} theme={theme} />}
            </section>
          </div>
        </div>
      </main>
    </div>
  )
}
