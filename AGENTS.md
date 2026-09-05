# MiniDB — Contexto del proyecto

> Archivo de contexto para sesiones de trabajo (humano o agente).
> Resume qué es el proyecto, cómo se trabaja en él y qué decisiones ya están tomadas.

## Qué es

Motor de base de datos multimodal construido **desde cero** en Python para el curso
**CS2042 — Bases de Datos II (UTEC)**, proyecto parcial (semana 8). Sin librerías
de BD ni ORMs: todo sobre páginas de 4 KB como exige el curso.

- **Parte 1 (relacional):** slotted pages, heap file con free list, serialización
  `struct`, índices B+ Tree y Hash Extensible, parser SQL propio, ejecutor con plan.
- **Parte 2 (espacial):** R-Tree (split cuadrático, búsqueda por radio, KNN con
  cola de prioridad), tipo `POINT`, mapa web con Leaflet.

## Stack y layout

- `backend/` — motor + API FastAPI. Tests: `cd backend && .venv/bin/python -m pytest tests/ -q`
  (125 tests al 2026-09-04).
- `frontend/` — React + Vite + Tailwind v4 + react-leaflet. Tema "Seline"
  (stone + cyan, modo claro/oscuro con toggle sol/luna).
- `datasets/` — CSVs de ejemplo (`CREATE TABLE t FROM FILE "x.csv"` lee de aquí).
- Raíz: `docker-compose.yml`, `.env` (gitignoreado, contiene `CARTO_API_KEY`),
  `.env.example`.

## Cómo se trabaja

- Todo se levanta con `docker compose up -d --build` desde la raíz.
  Web: http://localhost:5173 — API: :8000 (nginx proxea `/api`).
- **Tras cualquier cambio de código hay que rebuildear** (`--build`): el
  Dockerfile copia `app/`/`dist/` dentro de la imagen; `up -d` solo reutiliza la vieja.
- Host Fedora con SELinux: los bind mounts de compose llevan `:z`.
- Datos en el volumen Docker `minidb-data` (`/data` en el backend); sobreviven a
  `docker compose down`, se borran con `down -v`.
- Repo: `git@github.com:brandoLC/proyecto_BD2.git` (rama `main`).
  Identidad git: brandoLC / brando.lopez@utec.edu.pe.

## Decisiones ya tomadas (no reabrir)

- PK de una sola columna; al crearla genera B+ Tree automático. Se rechaza
  `CREATE INDEX BTREE` duplicado sobre la PK.
- Inferencia CSV: valida tipos numéricos contra todo el archivo, nunca infiere
  INT/FLOAT para números con cero a la izquierda; VARCHAR = max del archivo +20%
  (TEXT si excede 255); detecta pares lat/lng y deriva columna `location POINT`.
- Cabeceras CSV con tildes/espacios: `sanitize_identifier` las traduce
  ("Código Modular" → `c_digo_modular`) tanto al inferir como al cargar
  (`map_columns` normaliza igual; colisiones tras sanitizar = error claro).
- Las filas con errores se rechazan indicando línea y razón; el resto carga.
- Existe `DROP TABLE`. No existe `SERIAL`, `UNIQUE`, ni identificadores con comillas.
- **No hay NULL en ninguna capa**: celda vacía en columna numérica = fila
  rechazada. Workaround: tipar códigos como VARCHAR.
- Mapa: tiles CARTO con key (`light_all` claro / `dark_all` oscuro) sin
  watermark; sin key cae a OSM. La key viaja .env → build arg → `VITE_CARTO_API_KEY`.

## Estado de datos (volumen Docker)

- `big_test` (60k filas, BTREE + RTREE) — demo de rendimiento.
- `fast_food_restaurants_usa` (10k, PK `keys`, `location` POINT derivada).
- `netflix_titles` (8807 filas).
- CSVs del usuario en `~/Descargas/` (fast_food, netflix, instituciones Amazonas).

## Ideas pendientes (NO pedidas aún — esperar al usuario)

1. **Surrogate key autogenerada:** wizard detecta columnas únicas; si ninguna,
   propone `id INT PRIMARY KEY` que el loader rellena secuencial (como AUTO_INCREMENT).
   Backend ~60 líneas + tests, frontend checkbox.
2. **NULL real:** null bitmap en el registro (tema del curso), parser `NULL` /
   `IS NULL`, índices sin indexar nulos, loader vacío → None. ~200 líneas, toca
   formato de storage (recrear tablas o versionar formato).
3. **Deploy AWS:** EC2 t3.small + Elastic IP + SG (22 restringido, 5173 web),
   Docker + clone + `.env` + `compose up -d --build`. Crédito disponible $120.
   Ojo: la app no tiene auth — cualquiera con la IP puede hacer DROP TABLE.
4. **mapcn** (mapas React bonitos) — pospuesto, "si sobra tiempo".
5. Mejoras SQL mencionadas: AND/OR en WHERE, LIKE, ORDER BY (external sort),
   GROUP BY (external hashing), bulk-loading STR del R-Tree (build RTREE en 60k
   puntos tarda ~4 min — dato para la demo).
