# MiniDB — Frontend

Cliente web SQL para el mini motor de base de datos **MiniDB** (CS2042 — Bases de Datos II, UTEC).

Stack: React + Vite + Tailwind CSS v4 + Leaflet (react-leaflet).

## Requisitos

- Node.js 20+ y npm (para desarrollo)
- Backend FastAPI de MiniDB corriendo en `http://localhost:8000`

## Desarrollo

```bash
npm install
npm run dev
```

La app queda en `http://localhost:5173`. El servidor de desarrollo proxea
`/api` → `http://localhost:8000` (ver `vite.config.js`), así que basta con
levantar el backend en el puerto 8000.

Otros scripts:

```bash
npm run build    # build de producción en dist/
npm run preview  # sirve el build localmente
```

## Docker

El `Dockerfile` hace un build multi-etapa (Node → nginx) y sirve la SPA en el
puerto 80, proxeando `/api/` al servicio `backend` de docker-compose
(`http://backend:8000`, ver `nginx.conf`).

```bash
docker build -t minidb-frontend .
docker run -p 8080:80 minidb-frontend
```

> Nota: fuera de docker-compose, el proxy a `backend:8000` no resuelve; para
> probar de forma aislada ajusta el `proxy_pass` en `nginx.conf` o usa la red
> de tu compose. Uso típico con docker-compose:

```yaml
services:
  backend:
    # ... imagen del backend, escucha en :8000
  frontend:
    build: ./frontend
    ports:
      - "8080:80"
    depends_on:
      - backend
```

## Modo oscuro

El botón sol/luna de la barra superior alterna entre modo claro y oscuro.
La elección se guarda en `localStorage` (`theme: "dark" | "light"`); en la
primera visita se sigue `prefers-color-scheme`. Un script inline en
`index.html` aplica la clase `dark` en `<html>` antes del primer paint para
evitar destellos. Los tokens de color (`@theme` en `src/index.css`) se
redefinen bajo el selector `.dark`, y los tiles del mapa cambian de
OpenStreetMap a CartoDB Dark Matter.

## Flujo CSV

Hay dos formas de trabajar con archivos CSV (ambas usan `multipart/form-data`):

- **Nuevo desde CSV** (botón junto al título "Archivos"): sube un CSV a
  `POST /api/infer-schema`. Muestra una tarjeta con las columnas detectadas
  (nombre + tipo, PK resaltada en cyan), hasta 5 filas de muestra y la
  estimación total de filas. El `CREATE TABLE` sugerido se carga en el editor
  SQL para revisarlo/editarlo y ejecutarlo; luego se usa "Cargar CSV" en la
  tabla creada.
- **Cargar CSV** (botón en cada tabla): sube un CSV a
  `POST /api/tables/{nombre}/upload-csv`. Al terminar refresca la lista de
  tablas y muestra el resultado: filas cargadas y tiempo, los primeros errores
  de filas rechazadas (lista expandible) y las columnas ignoradas, si las
  hubiera.

## Estructura

```
src/
  api.js                      # wrappers fetch (/api/health, /api/tables, /api/query,
                              #  /api/infer-schema, /api/tables/{n}/upload-csv)
  App.jsx                     # layout principal y estado global
  components/
    TopNav.jsx                # barra superior + estado de conexión
    Sidebar.jsx               # tablas, columnas, índices, archivos físicos y carga CSV
    InferSchemaPanel.jsx      # previsualización del esquema inferido de un CSV
    CsvUploadStatus.jsx       # resultado de la carga CSV por tabla
    SqlEditor.jsx             # editor SQL, ejemplos, Ejecutar/Limpiar
    StatusMessage.jsx         # errores de la API con badge de etapa
    ResultsTable.jsx          # tabla de resultados
    PlanPanel.jsx             # plan de ejecución por pasos
    MapPanel.jsx              # mapa Leaflet con puntos espaciales
```
