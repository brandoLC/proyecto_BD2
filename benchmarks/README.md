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
backend/.venv/bin/python benchmarks/exp3_rangos.py
backend/.venv/bin/python benchmarks/exp4_bloque.py
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

### `exp3_rangos.py` — búsquedas por rango con selectividad variable

N = 100 000 registros en tres variantes (heap sin índice, heap + B+ Tree,
sequential reorganizada). Rangos `WHERE id BETWEEN a AND b` del 0.1%, 1%,
5%, 10% y 25% del total, en posiciones aleatorias con semilla fija (las
mismas para las tres variantes). Promedio ± std de `io.reads` y latencia.

- Las consultas llevan `LIMIT 100000` explícito para que el tope de
  seguridad SELECT_ROW_CAP no corte el barrido del rango.
- El full scan se mide con 10 consultas por selectividad; las demás con
  50 (columna `consultas`).
- Sirve para observar el punto de cruce: la selectividad a partir de la
  cual el full scan gana al B+ Tree (los RIDs del rango están dispersos
  en el heap, así que el fetch por índice termina leyendo casi todas las
  páginas).

### `exp4_bloque.py` — sensibilidad del B+ Tree al tamaño de bloque

B+ Tree aislado (sin heap ni Engine) con N = 100 000 claves INT,
instanciado directamente con `BPlusTree(..., page_size=B)` para
B ∈ [1024, 2048, 4096, 8192]. Por corrida: capacidad máxima de nodo hoja
e interno (según el formato de serialización), altura del árbol
(`tree.height()`), páginas/bytes del archivo, tiempo de carga y lecturas
de 1000 búsquedas puntuales con la caché de nodos vaciada antes de cada
consulta (`t._cache.clear()`) para medir el peor caso (cada nivel = 1
lectura física).

- Es el único benchmark que no usa `Engine`: habla con el índice
  directamente, como los tests unitarios.
- Resultado de referencia: fan-out interno 72 → 145 → 292 → 584, altura
  3,3,3,2 y reads de búsqueda 3.0 → 2.0 al duplicar B hasta 8 KB.
