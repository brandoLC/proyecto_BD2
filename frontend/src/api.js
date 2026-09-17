// Wrappers mínimos contra la API de MiniDB (contrato fijado).

async function parseResponse(res) {
  const data = await res.json().catch(() => null)
  if (data === null) {
    throw new Error(`Respuesta no JSON del servidor (HTTP ${res.status})`)
  }
  return data
}

export async function getHealth() {
  const res = await fetch('/api/health')
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return parseResponse(res)
}

export async function getTables() {
  const res = await fetch('/api/tables')
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return parseResponse(res)
}

// POST multipart: el backend puede devolver {ok:false,...} con código de error HTTP.
async function postForm(url, form) {
  const res = await fetch(url, { method: 'POST', body: form })
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    if (data) return data
    throw new Error(`HTTP ${res.status}`)
  }
  return parseResponse(res)
}

export async function inferSchema(file, tableName) {
  const form = new FormData()
  form.append('file', file)
  if (tableName) form.append('table_name', tableName)
  return postForm('/api/infer-schema', form)
}

// derivedPoint (opcional): {column, lat_col, lng_col} para que el backend
// derive una columna POINT a partir de dos columnas del CSV.
export async function uploadCsv(tableName, file, derivedPoint) {
  const form = new FormData()
  form.append('file', file)
  if (derivedPoint) {
    form.append('point_column', derivedPoint.column)
    form.append('lat_col', derivedPoint.lat_col)
    form.append('lng_col', derivedPoint.lng_col)
  }
  return postForm(`/api/tables/${encodeURIComponent(tableName)}/upload-csv`, form)
}

// POST reorganize: compacta el archivo sequential de una tabla.
export async function reorganizeTable(tableName) {
  const res = await fetch(`/api/tables/${encodeURIComponent(tableName)}/reorganize`, {
    method: 'POST',
  })
  if (!res.ok) {
    // 404 si no existe, 409 si no es sequential; puede venir {ok:false}
    // o el {"detail": ...} de FastAPI.
    const data = await res.json().catch(() => null)
    if (data?.ok === false) return data
    if (data?.detail) {
      return {
        ok: false,
        error: typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail),
      }
    }
    throw new Error(`HTTP ${res.status}`)
  }
  return parseResponse(res)
}

export async function postQuery(sql) {
  const res = await fetch('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql }),
  })
  if (!res.ok) {
    // El backend puede devolver {ok:false,...} con código de error HTTP.
    const data = await res.json().catch(() => null)
    if (data) return data
    throw new Error(`HTTP ${res.status}`)
  }
  return parseResponse(res)
}
