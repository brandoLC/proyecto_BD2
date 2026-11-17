// Vista Recomendador: búsqueda de productos por foto (embeddings IVF o
// BoVW clásico) o por texto (TF-IDF coseno), con filtros comerciales que
// ejercitan los cuatro índices del motor (B+ precio, R-Tree tienda,
// índice invertido, IVF). Muestra el SQL generado y el plan de ejecución.
import { useCallback, useEffect, useRef, useState } from 'react'
import { MapContainer, TileLayer, CircleMarker, Circle, useMapEvents } from 'react-leaflet'
import {
  getRecommendSample,
  productImageUrl,
  recommendImage,
  recommendText,
} from '../api.js'
import PlanPanel from './PlanPanel.jsx'
import { TILES } from './MapPanel.jsx'

const LIMA = [-12.0464, -77.0428]
const ACCENT = '#3ba6f1'

// Captura clicks sobre el mapa para fijar el centro de la búsqueda.
function ClickPicker({ onPick }) {
  useMapEvents({
    click(e) {
      onPick({ lat: e.latlng.lat, lng: e.latlng.lng })
    },
  })
  return null
}

function Field({ label, children }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-body">{label}</span>
      {children}
    </label>
  )
}

const inputCls =
  'rounded-input border border-hairline bg-canvas px-2.5 py-1.5 text-sm text-ink ' +
  'placeholder:text-helper focus:border-accent focus:outline-none'

function ProductCard({ product, rankLabel }) {
  return (
    <article className="overflow-hidden rounded-card border border-hairline bg-surface shadow-card">
      <img
        src={productImageUrl(product.id)}
        alt={product.nombre}
        loading="lazy"
        className="h-40 w-full bg-canvas object-cover"
      />
      <div className="flex flex-col gap-1 p-3">
        <div className="flex items-start justify-between gap-2">
          <p className="font-heading text-sm font-medium leading-tight text-ink">
            {product.nombre}
          </p>
          {rankLabel && (
            <span className="shrink-0 rounded-full border border-accent px-2 py-0.5 font-mono text-[11px] text-accent-edge">
              {rankLabel}
            </span>
          )}
        </div>
        <p className="text-xs text-helper">{product.categoria}</p>
        <div className="mt-1 flex items-baseline justify-between">
          <span className="font-heading text-sm font-medium text-ink">
            S/ {Number(product.precio).toFixed(2)}
          </span>
          <span className="max-w-[55%] truncate text-[11px] text-body" title={product.tienda_nombre}>
            {product.tienda_nombre}
          </span>
        </div>
      </div>
    </article>
  )
}

export default function RecommenderPanel({ theme = 'light' }) {
  const [queryType, setQueryType] = useState('image') // 'image' | 'text'
  const [mode, setMode] = useState('embedding') // 'embedding' | 'bovw'
  const [file, setFile] = useState(null)
  const [previewUrl, setPreviewUrl] = useState(null)
  const [text, setText] = useState('')
  const [k, setK] = useState(8)
  const [probes, setProbes] = useState('')
  const [precioMin, setPrecioMin] = useState('')
  const [precioMax, setPrecioMax] = useState('')
  const [categoria, setCategoria] = useState('')
  const [geoEnabled, setGeoEnabled] = useState(false)
  const [center, setCenter] = useState(null) // {lat, lng} elegido en el mapa
  const [radioKm, setRadioKm] = useState(5)

  const [config, setConfig] = useState(null) // app_config.json del backend
  const [sample, setSample] = useState([])
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null) // respuesta ok del recomendador
  const [error, setError] = useState(null) // {error, stage}
  const fileInputRef = useRef(null)

  useEffect(() => {
    getRecommendSample(8)
      .then((data) => {
        if (data.ok) {
          setConfig(data.config)
          setSample(data.products || [])
        }
      })
      .catch(() => {})
  }, [])

  const pickFile = useCallback(
    (f) => {
      setFile(f || null)
      if (previewUrl) URL.revokeObjectURL(previewUrl)
      setPreviewUrl(f ? URL.createObjectURL(f) : null)
    },
    [previewUrl],
  )

  const submit = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const filters = {
        k,
        precio_min: precioMin,
        precio_max: precioMax,
        categoria: categoria || null,
        lat: geoEnabled && center ? center.lat : null,
        lng: geoEnabled && center ? center.lng : null,
        radio_km: geoEnabled && center ? radioKm : null,
      }
      let data
      if (queryType === 'image') {
        if (!file) {
          setError({ error: 'Sube una foto de consulta primero.', stage: null })
          return
        }
        data = await recommendImage(file, {
          ...filters,
          mode,
          probes: mode === 'embedding' && probes !== '' ? probes : null,
        })
      } else {
        data = await recommendText({ ...filters, text })
      }
      if (data.ok) {
        setResult(data)
      } else {
        setError({ error: data.error || 'Error desconocido', stage: data.stage })
      }
    } catch (e) {
      setError({ error: `Error de red: ${e.message}`, stage: null })
    } finally {
      setLoading(false)
    }
  }, [queryType, file, mode, probes, text, k, precioMin, precioMax,
      categoria, geoEnabled, center, radioKm])

  const categories = config?.categories || []
  const pill = (active) =>
    active
      ? 'rounded-full bg-dark px-3 py-1 text-xs text-white dark:text-canvas'
      : 'rounded-full border border-hairline px-3 py-1 text-xs text-ink transition-colors hover:bg-canvas'

  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[320px_1fr]">
      {/* ------------------- controles ------------------- */}
      <section className="flex flex-col gap-4 rounded-card border border-hairline bg-surface p-5 shadow-card">
        <div>
          <h2 className="font-heading text-base font-medium text-ink">
            Recomendador e-commerce
          </h2>
          <p className="mt-0.5 text-xs text-helper">
            {config
              ? `${config.products} productos · BoVW K=${config.bovw_words} · ` +
                `embeddings ${config.embedding_dim}D`
              : 'Dataset no preparado: ejecuta scripts/prepare_recommender.py'}
          </p>
        </div>

        <div className="flex gap-2">
          <button type="button" onClick={() => setQueryType('image')} className={pill(queryType === 'image')}>
            Por foto
          </button>
          <button type="button" onClick={() => setQueryType('text')} className={pill(queryType === 'text')}>
            Por texto
          </button>
        </div>

        {queryType === 'image' ? (
          <>
            <div className="flex gap-2">
              <button type="button" onClick={() => setMode('embedding')} className={pill(mode === 'embedding')}>
                Embeddings · IVF
              </button>
              <button type="button" onClick={() => setMode('bovw')} className={pill(mode === 'bovw')}>
                BoVW clásico
              </button>
            </div>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="flex min-h-28 items-center justify-center overflow-hidden rounded-input border border-dashed border-border-strong bg-canvas text-xs text-helper transition-colors hover:border-accent hover:text-body"
            >
              {previewUrl ? (
                <img src={previewUrl} alt="consulta" className="h-full w-full object-contain" />
              ) : (
                'Haz clic para subir una foto de consulta'
              )}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => pickFile(e.target.files?.[0])}
            />
            {mode === 'embedding' && (
              <Field label="probes del IVF (vacío = 1)">
                <input
                  type="number" min="1" value={probes}
                  onChange={(e) => setProbes(e.target.value)}
                  placeholder="1" className={inputCls}
                />
              </Field>
            )}
          </>
        ) : (
          <Field label="Describe lo que buscas">
            <textarea
              rows={4} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="p. ej. polo rojo deportivo para hombre"
              className={inputCls}
            />
          </Field>
        )}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Precio mín. (S/)">
            <input type="number" min="0" value={precioMin}
              onChange={(e) => setPrecioMin(e.target.value)} className={inputCls} />
          </Field>
          <Field label="Precio máx. (S/)">
            <input type="number" min="0" value={precioMax}
              onChange={(e) => setPrecioMax(e.target.value)} className={inputCls} />
          </Field>
        </div>

        <Field label="Categoría">
          <select value={categoria} onChange={(e) => setCategoria(e.target.value)} className={inputCls}>
            <option value="">Todas</option>
            {categories.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </Field>

        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-xs font-medium text-body">
            <input
              type="checkbox" checked={geoEnabled}
              onChange={(e) => {
                setGeoEnabled(e.target.checked)
                if (e.target.checked && !center) {
                  setCenter({ lat: LIMA[0], lng: LIMA[1] })
                }
              }}
            />
            Filtrar por cercanía de tienda (R-Tree)
          </label>
          {geoEnabled && (
            <>
              <div className="overflow-hidden rounded-input border border-hairline" style={{ height: 180 }}>
                <MapContainer center={LIMA} zoom={12} style={{ height: '100%', width: '100%' }}>
                  <TileLayer key={theme} attribution={TILES[theme].attribution} url={TILES[theme].url} />
                  <ClickPicker onPick={setCenter} />
                  {center && (
                    <>
                      <Circle
                        center={[center.lat, center.lng]}
                        radius={radioKm * 1000}
                        pathOptions={{ color: ACCENT, weight: 1.5, fillOpacity: 0.08 }}
                      />
                      <CircleMarker
                        center={[center.lat, center.lng]} radius={6}
                        pathOptions={{ color: ACCENT, fillColor: ACCENT, fillOpacity: 0.9 }}
                      />
                    </>
                  )}
                </MapContainer>
              </div>
              <Field label={`Radio: ${radioKm} km`}>
                <input
                  type="range" min="1" max="20" step="1" value={radioKm}
                  onChange={(e) => setRadioKm(Number(e.target.value))}
                />
              </Field>
            </>
          )}
        </div>

        <Field label={`Resultados: top ${k}`}>
          <input
            type="range" min="4" max="24" step="4" value={k}
            onChange={(e) => setK(Number(e.target.value))}
          />
        </Field>

        <button
          type="button" onClick={submit} disabled={loading}
          className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
        >
          {loading ? 'Buscando…' : 'Recomendar'}
        </button>

        {error && (
          <p className="rounded-input border border-error/40 bg-error/5 px-3 py-2 text-xs text-error">
            {error.error}
            {error.stage && <span className="ml-1 font-mono">[{error.stage}]</span>}
          </p>
        )}
      </section>

      {/* ------------------- resultados ------------------- */}
      <section className="flex min-w-0 flex-col gap-5">
        <div className="rounded-card border border-hairline bg-surface p-5 shadow-card">
          {result ? (
            <>
              <div className="mb-3 flex items-baseline justify-between">
                <h3 className="font-heading text-sm font-medium text-ink">
                  {result.products.length} recomendaciones
                  <span className="ml-2 font-mono text-xs font-normal text-helper">
                    {result.mode === 'bovw'
                      ? 'BoVW + índice invertido'
                      : result.mode === 'text'
                        ? 'texto + índice invertido'
                        : 'embedding + IVF'}
                    {' · '}{result.elapsed_ms} ms
                    {' · I/O '}{result.io.reads}/{result.io.writes}
                  </span>
                </h3>
              </div>
              {result.products.length === 0 ? (
                <p className="py-10 text-center text-xs text-helper">
                  Ningún producto cumple los filtros.
                </p>
              ) : (
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
                  {result.products.map((p) => (
                    <ProductCard
                      key={p.id} product={p}
                      rankLabel={
                        p.dist != null
                          ? `d=${p.dist}`
                          : p.score != null
                            ? `s=${p.score}`
                            : null
                      }
                    />
                  ))}
                </div>
              )}
            </>
          ) : sample.length > 0 ? (
            <>
              <h3 className="mb-3 font-heading text-sm font-medium text-ink">
                Muestra del catálogo
              </h3>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
                {sample.map((p) => (
                  <ProductCard key={p.id} product={p} rankLabel={null} />
                ))}
              </div>
            </>
          ) : (
            <p className="py-10 text-center text-xs text-helper">
              Ejecuta una recomendación para ver resultados.
            </p>
          )}
        </div>

        {result?.sql && (
          <div className="rounded-card border border-hairline bg-surface p-5 shadow-card">
            <h3 className="mb-2 font-heading text-sm font-medium text-ink">
              SQL ejecutado en MiniDB
            </h3>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-input bg-canvas p-3 font-mono text-xs text-body">
              {result.sql}
            </pre>
          </div>
        )}

        {result && (
          <div className="rounded-card border border-hairline bg-surface p-5 shadow-card">
            <h3 className="mb-2 font-heading text-sm font-medium text-ink">
              Plan de ejecución
            </h3>
            <PlanPanel result={result} />
          </div>
        )}
      </section>
    </div>
  )
}
