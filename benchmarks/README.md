# Benchmarks — MiniDB (CS2042, proyecto parcial)

Scripts de benchmark del motor. Usan el ``Engine`` directamente (sin HTTP)
con instancias aisladas en directorios temporales, y la I/O física se mide
con el ``DiskCounter`` integrado (``result["io"]`` por consulta, o activado
manualmente alrededor de ``bulk_load_rows``).

## Cómo correrlos

Desde la raíz del repo, con el venv del backend:

```bash
backend/.venv/bin/python benchmarks/exp1_insercion.py
backend/.venv/bin/python benchmarks/exp2_busquedas.py
```

Los resultados se guardan en `benchmarks/results/` (gitignoreado) como CSV
y además se imprime una tabla resumen por consola.

## Qué mide cada uno

### `exp1_insercion.py` — inserción masiva

N ∈ [1 000, 10 000, 50 000, 100 000] registros
`(id INT PK, nombre VARCHAR(30), valor FLOAT)` en cuatro organizaciones:
heap sin índice, heap + B+ Tree (PK), heap + Hash Extensible (PK) y
Sequential File. Por corrida: tiempo total (s), lecturas/escrituras de
páginas de 4 KB y filas/seg.

- La carga es en lote (`bulk_load_rows`, el mismo camino físico de la
  carga CSV), no INSERT tupla a tupla.
- Las variantes heap reciben claves barajadas (semilla fija); sequential
  recibe claves ordenadas por la PK (camino rápido O(1) del tail — con
  100k tuplas desordenadas la cadena de overflow lo haría O(N²)).
- Los N grandes (250k/500k) quedan comentados como opcionales.

### `exp2_busquedas.py` — búsquedas puntuales

Sobre N = 100 000 registros precargados por organización: búsquedas de
igualdad `WHERE id = k` con claves aleatorias existentes. Reporta promedio
y desviación estándar de `io.reads` y de latencia (ms) por consulta.

- La tabla sequential se reorganiza tras la carga (operación periódica
  normal de esta organización) para medir la búsqueda binaria sobre el
  área principal ordenada.
- El full scan (~0.3 s/consulta sobre 100k) se mide con 100 consultas;
  las demás variantes con 1000 (columna `consultas` del CSV).
