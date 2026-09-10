// Pestaña Mapa: puntos espaciales sobre OpenStreetMap (Lima por defecto).
import { useEffect, useState } from 'react'
import L from 'leaflet'
import { MapContainer, TileLayer, CircleMarker, Circle, Popup, useMap } from 'react-leaflet'

const LIMA = [-12.0464, -77.0428]
const ACCENT = '#3ba6f1'

// API key de CARTO Basemaps (raster): se inyecta en build con
// VITE_CARTO_API_KEY (ver .env en la raíz y el build arg del Dockerfile).
// Sin key los tiles oscuros funcionan pero muestran marca de agua.
const CARTO_KEY = import.meta.env.VITE_CARTO_API_KEY || ''
const cartoUrl = (style) =>
  `https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png` +
  (CARTO_KEY ? `?key=${CARTO_KEY}` : '')
// Voyager solo existe en el servicio raster con key: /rastertiles/voyager/
// (sin subdominio {s}); en el path /voyager/ da 404.
const VOYAGER_URL =
  `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png` +
  (CARTO_KEY ? `?key=${CARTO_KEY}` : '')

// Tiles según el tema: CARTO Voyager (a color) en claro, CartoDB Dark
// Matter en oscuro. Con key de CARTO van sin marca de agua; sin key, el
// modo claro cae a OSM (gratis, sin watermark) y el oscuro queda con
// watermark.
const TILES = {
  light: {
    url: CARTO_KEY
      ? VOYAGER_URL
      : 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: CARTO_KEY
      ? '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
      : '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  },
  dark: {
    url: cartoUrl('dark_all'),
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  },
}

// Detecta "WHERE <col> IN ((x, y), r)" para dibujar el círculo de búsqueda.
function parseRadius(sql) {
  if (!sql) return null
  const m = sql.match(
    /IN\s*\(\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)\s*,\s*(\d+(?:\.\d+)?)\s*\)/i,
  )
  if (!m) return null
  return { center: [Number(m[1]), Number(m[2])], radius: Number(m[3]) }
}

// Encuadra el mapa en los puntos (y el círculo de búsqueda, si existe) cada
// vez que cambia el conjunto de resultados, en lugar de quedarse en Lima.
function FitBounds({ points, radiusCircle }) {
  const map = useMap()
  const first = points[0]
  // Firma simple del resultado: cantidad + primer punto (+ círculo).
  const signature = `${points.length}:${first ? `${first.x},${first.y}` : ''}${
    radiusCircle ? `|${radiusCircle.center}|${radiusCircle.radius}` : ''
  }`

  useEffect(() => {
    const bounds = L.latLngBounds(points.map((p) => [p.x, p.y]))
    if (radiusCircle) {
      // El radio viene en grados; se expande el encuadre para incluir el círculo.
      const [cx, cy] = radiusCircle.center
      const r = radiusCircle.radius
      bounds.extend([cx - r, cy - r])
      bounds.extend([cx + r, cy + r])
    }
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, signature])

  return null
}

export default function MapPanel({ result, sql, theme = 'light' }) {
  const [expanded, setExpanded] = useState(false)
  const spatial = result?.spatial

  // Cerrar el mapa ampliado con Escape.
  useEffect(() => {
    if (!expanded) return
    const onKey = (e) => {
      if (e.key === 'Escape') setExpanded(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded])

  if (!spatial || !spatial.points || spatial.points.length === 0) {
    return (
      <p className="py-10 text-center text-xs text-helper">
        Ejecuta una consulta espacial para ver el mapa.
      </p>
    )
  }

  const radius = parseRadius(sql)

  const map = (
    // key: fuerza el remontaje al cambiar de tamaño (Leaflet se recalcula
    // limpio en el contenedor nuevo, sin invalidateSize manual).
    <MapContainer
      key={expanded ? 'expanded' : 'inline'}
      center={LIMA}
      zoom={13}
      style={{ height: '100%', width: '100%' }}
    >
      <FitBounds points={spatial.points} radiusCircle={radius} />
      <TileLayer
        // key por tema: fuerza el remontaje de la capa al cambiar claro/oscuro.
        key={theme}
        attribution={TILES[theme].attribution}
        url={TILES[theme].url}
      />
      {radius && (
        <Circle
          center={radius.center}
          // El radio viene en grados; aproximación a metros (1° ≈ 111 320 m).
          radius={radius.radius * 111320}
          pathOptions={{ color: ACCENT, weight: 1.5, fillOpacity: 0.08 }}
        />
      )}
      {spatial.points.map((p, i) => (
        <CircleMarker
          key={i}
          center={[p.x, p.y]}
          radius={7}
          pathOptions={{ color: ACCENT, fillColor: ACCENT, fillOpacity: 0.85, weight: 2 }}
        >
          <Popup>
            <div className="font-mono text-xs">
              <p className="mb-1 font-medium">
                ({p.x}, {p.y})
              </p>
              {p.row && p.row.length > 0 && (
                <ul>
                  {p.row.map((cell, j) => (
                    <li key={j}>{String(cell)}</li>
                  ))}
                </ul>
              )}
            </div>
          </Popup>
        </CircleMarker>
      ))}
    </MapContainer>
  )

  return (
    <>
      <div className="relative overflow-hidden rounded-input border border-hairline">
        <div style={{ height: '420px' }}>{map}</div>
        <button
          onClick={() => setExpanded(true)}
          title="Ampliar mapa"
          className="absolute right-2.5 top-2.5 z-[1000] rounded-full border border-hairline bg-surface px-2.5 py-1 text-xs font-medium text-body shadow-card transition-colors hover:bg-canvas hover:text-ink"
        >
          ⤢ Ampliar
        </button>
      </div>

      {expanded && (
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60"
          onClick={() => setExpanded(false)}
        >
          <div
            className="relative h-[90vh] w-[92vw] overflow-hidden rounded-card border border-hairline bg-surface shadow-card"
            onClick={(e) => e.stopPropagation()}
          >
            {map}
            <button
              onClick={() => setExpanded(false)}
              title="Cerrar (Escape)"
              className="absolute right-2.5 top-2.5 z-[1000] rounded-full border border-hairline bg-surface px-2.5 py-1 text-xs font-medium text-body shadow-card transition-colors hover:bg-canvas hover:text-ink"
            >
              ✕ Cerrar
            </button>
          </div>
        </div>
      )}
    </>
  )
}
