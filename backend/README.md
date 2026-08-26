# MiniDB — Backend

Mini motor de base de datos multimodal para el curso **CS2042 Bases de
Datos II (UTEC)**. Todo está implementado **desde cero** (sin librerías
de bases de datos ni ORMs) sobre **páginas de 4 KB**, tal como exigen las
clases del curso:

- **Parte 1**: almacenamiento relacional (páginas ranuradas, heap file
  con lista de libres, serialización con `struct`) e índices **B+ Tree**
  y **Hash Extensible**.
- **Parte 2**: índice espacial **R-Tree** (split cuadrático, búsqueda por
  radio con poda por MBR y KNN con cola de prioridad).

## Arquitectura

```
SQL → parser (tokenizer + recursivo descendente, AST)
    → executor (validación semántica, plan de ejecución con tiempos)
    → storage (páginas ranuradas 4 KB, heap file, serialización struct)
      / indexes (B+ Tree, Hash Extensible, R-Tree, cada uno en su archivo)
    → disco (directorio DATA_DIR: <tabla>.heap, <tabla>_<col>.btree|hash|rtree)
```

- `app/storage/page.py` — `SlottedPage` de 4096 bytes: cabecera
  (número de slots, puntero de espacio libre), slot array de
  (offset, longitud, flag vivo) y tuplas escritas desde el final de la
  página hacia atrás. `delete` marca el slot como muerto (el `slot_id`
  permanece estable) y `compact()` elimina la fragmentación.
- `app/storage/record.py` — serialización con `struct`:
  `INT='i'`, `FLOAT='d'`, `BOOL='?'`, `VARCHAR(n)='{n}s'`, `POINT='dd'`,
  `TEXT` con prefijo de longitud. Registro de longitud fija si no hay
  columnas `TEXT`.
- `app/storage/heap_file.py` — archivo de páginas de 4 KB. La página 0
  guarda metadatos y la cabeza de la lista de libres. RID =
  `(page_id, slot_id)`. Insertar reutiliza primero slots libres; borrar
  marca el slot y lo empuja a la lista de libres.
- `app/indexes/btree.py` — B+ Tree persistido en su propio archivo de
  páginas; el orden se calcula para que cada nodo quepa en 4 KB. Soporta
  claves duplicadas con entradas `(key, rid)`. Operaciones: `insert`,
  `search`, `range_search` (con extremos abiertos) y `delete` (sin
  fusión de nodos: se tolera underflow, simplificación didáctica).
- `app/indexes/extendible_hash.py` — directorio con profundidad global +
  buckets de capacidad fija, duplicación del directorio al desbordarse.
  Función hash FNV-1a implementada a mano.
- `app/indexes/rtree.py` — R-Tree con split cuadrático,
  `search_radius` (poda por MBR + filtro de distancia exacta) y `knn`
  (min-heap sobre la distancia mínima al MBR). Eliminación sin
  reinserción en underflow (simplificación didáctica).
- `app/engine/catalog.py` — catálogo del sistema en `catalog.json`
  (esquemas de tablas e índices), persistido en cada DDL.
- `app/engine/parser.py` — tokenizer + parser recursivo descendente;
  errores de sintaxis con posición.
- `app/engine/executor.py` — ejecuta el AST y devuelve el resultado más
  una lista `plan` de pasos con tiempos (`time_ms`), eligiendo
  *Index Scan* cuando hay índice usable y *Sequential Scan* si no.

## Gramática SQL soportada

```sql
CREATE TABLE nombre (col TIPO [PRIMARY KEY], ...);
-- TIPO: INT | FLOAT | VARCHAR(n) | TEXT | BOOL | POINT  (POINT = dos floats x,y)

CREATE INDEX [nombre] ON tabla (col) USING BTREE|HASH|RTREE;
-- RTREE solo sobre columnas POINT; BTREE/HASH sobre columnas escalares

INSERT INTO t VALUES (v1, v2, ...);          -- un literal POINT se escribe (x, y)

SELECT * | c1, c2 FROM t [WHERE cond] [LIMIT n];
-- cond:
--   col = lit | col < n | col <= n | col > n | col >= n
--   col BETWEEN a AND b
--   pointcol IN ((x, y), r)     -- radio espacial (R-Tree + distancia exacta)
--   pointcol KNN ((x, y), k)    -- k vecinos más cercanos (R-Tree + min-heap)

DELETE FROM t WHERE col = lit;
```

Keywords case-insensitive y `;` final opcional.

## Ejecutar localmente

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Los datos se persisten en `./data` (o en `$DATA_DIR` si está definida).

## Ejecutar con Docker

```bash
docker build -t minidb-backend .
docker run -p 8000:8000 minidb-backend
```

## Pruebas

```bash
python -m pytest tests/ -q
```

## API REST

- `GET /api/health` → `{"status": "ok"}`
- `GET /api/tables` → tablas con columnas, índices, `rowcount` y archivos
  (`path`, `size_bytes`, `pages`)
- `POST /api/query` con `{"sql": "..."}` → resultado con `ok`, `kind`,
  `columns`/`rows` (select), `rowcount`, `message`, `plan` (pasos con
  tiempos), `elapsed_ms` y `spatial` (puntos cuando el resultado incluye
  una columna POINT, si no `null`). En error: `{"ok": false, "error",
  "stage": "parse|semantic|execution"}`.

## Sesión de ejemplo

```bash
curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "CREATE TABLE restaurantes (id INT PRIMARY KEY, nombre VARCHAR(50), ubicacion POINT);"}'

curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "CREATE INDEX idx_id ON restaurantes (id) USING BTREE;"}'

curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "CREATE INDEX idx_ubi ON restaurantes (ubicacion) USING RTREE;"}'

curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "INSERT INTO restaurantes VALUES (1, '"'"'La Mar'"'"', (-12.06, -77.03));"}'

# Búsqueda por PK (Index Scan con BTREE)
curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT * FROM restaurantes WHERE id = 1;"}'

# Rango (B+ tree range search)
curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT * FROM restaurantes WHERE id BETWEEN 1 AND 10;"}'

# Radio espacial (R-Tree: poda por MBR + distancia exacta)
curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT * FROM restaurantes WHERE ubicacion IN ((-12.06, -77.03), 0.5);"}'

# KNN (R-Tree con cola de prioridad)
curl -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT * FROM restaurantes WHERE ubicacion KNN ((-12.06, -77.03), 3);"}'

curl localhost:8000/api/tables
```
