// Pestaña Mapa: puntos espaciales sobre OpenStreetMap (Lima por defecto).
import { MapContainer, TileLayer, CircleMarker, Circle, Popup } from 'react-leaflet'

const LIMA = [-12.0464, -77.0428]
const ACCENT = '#3ba6f1'

// Detecta "WHERE <col> IN ((x, y), r)" para dibujar el círculo de búsqueda.
function parseRadius(sql) {
  if (!sql) return null
  const m = sql.match(
    /IN\s*\(\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)\s*,\s*(\d+(?:\.\d+)?)\s*\)/i,
  )
  if (!m) return null
  return { center: [Number(m[1]), Number(m[2])], radius: Number(m[3]) }
}

export default function MapPanel({ result, sql }) {
  const spatial = result?.spatial

  if (!spatial || !spatial.points || spatial.points.length === 0) {
    return (
      <p className="py-10 text-center text-xs text-helper">
        Ejecuta una consulta espacial para ver el mapa.
      </p>
    )
  }

  const radius = parseRadius(sql)

  return (
    <div className="overflow-hidden rounded-input border border-hairline">
      <MapContainer center={LIMA} zoom={13} style={{ height: '420px', width: '100%' }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
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
    </div>
  )
}
