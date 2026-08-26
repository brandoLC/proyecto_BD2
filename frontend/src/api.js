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
