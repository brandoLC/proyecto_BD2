# MiniDB — Mini-gestor de bases de datos multimodal

Proyecto del curso **CS2042 — Bases de Datos II** (UTEC).

Motor de bases de datos construido **desde cero** en Python: almacenamiento en
páginas de 4 KB (slotted pages, heap file con free list), índices B+ Tree,
Hash Extensible y R-Tree, parser SQL propio y ejecutor con plan de consulta.
Se expone vía API REST (FastAPI) y se consume desde un cliente SQL web
(React + Vite + Tailwind v4 + Leaflet).

Estado actual: **Partes 1 y 2** (relacional + espacial) — entrega parcial, semana 8.

## Levantar todo con Docker Compose

```bash
docker compose up -d --build
```

- Cliente SQL web: <http://localhost:5173>
- API REST: <http://localhost:8000> (docs: <http://localhost:8000/docs>)

Los datos persisten en el volumen `minidb-data` (montado en `/data` del backend).

```bash
docker compose down        # detener
docker compose down -v     # detener y borrar los datos
```

## Estructura

```
backend/    Motor de BD (storage, índices, parser, ejecutor) + API FastAPI
frontend/   Cliente SQL web (editor, resultados, plan de ejecución, mapa)
```

Ver `backend/README.md` para la gramática SQL soportada y la arquitectura
interna, y `frontend/README.md` para el desarrollo del cliente.

## Ejemplo rápido

```sql
CREATE TABLE tiendas (id INT PRIMARY KEY, nombre VARCHAR(30), location POINT);
CREATE INDEX ON tiendas (location) USING RTREE;
INSERT INTO tiendas VALUES (1, 'Tienda Norte', (-12.04, -77.04));
SELECT * FROM tiendas WHERE location KNN ((-12.04, -77.04), 3);
SELECT id, nombre FROM tiendas WHERE location IN ((-12.04, -77.04), 0.02);
```

## Carga de datos desde CSV

Tres formas de cargar datos masivamente:

1. **Desde la web**: crea la tabla con `CREATE TABLE`, luego usa el botón
   **"Cargar CSV"** de la tabla en el panel de archivos y elige el archivo
   desde tu PC. Las filas con errores se rechazan indicando la línea y la razón.
2. **Asistente de esquema**: el botón **"Nuevo desde CSV"** analiza el archivo,
   infiere los tipos de columna y pre-llena el editor con el `CREATE TABLE`
   sugerido para que lo revises y ejecutes.
3. **Desde SQL**: `CREATE TABLE t FROM FILE "archivo.csv";` (infiere el esquema
   y carga) o `LOAD INTO t FROM FILE "archivo.csv";` (tabla existente). Los
   archivos se leen de la carpeta `./datasets` (montada en el backend).

Convención: el CSV lleva header con los nombres de columna y los puntos se
escriben entre comillas como `"(lat, lon)"`. Ver `datasets/` para ejemplos.

## Desarrollo local (sin Docker)

```bash
# Backend
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload          # http://localhost:8000

# Frontend
cd frontend && npm install
npm run dev                            # http://localhost:5173 (proxy /api → 8000)

# Tests del motor
cd backend && python -m pytest tests/ -q
```
