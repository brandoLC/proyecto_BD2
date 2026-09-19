"""Experimento 3: búsquedas por rango con selectividad variable.

Sobre tablas de N = 100 000 registros
``(id INT PK, nombre VARCHAR(30), valor FLOAT)`` en tres variantes:

- ``heap``:        full scan (sin índice sobre la PK),
- ``heap_btree``:  Index Range Scan con B+ Tree,
- ``sequential``:  búsqueda binaria + recorrido de cadena (reorganizada
                   tras la carga, operación periódica normal).

Consultas ``WHERE id BETWEEN a AND b`` con rangos del 0.1%, 1%, 5%, 10%
y 25% del total de tuplas, en posiciones aleatorias (semilla fija, las
mismas posiciones para las tres variantes). Por cada (variante,
selectividad): promedio ± std de ``io.reads`` y de latencia (ms).

Notas de metodología:

- Las consultas llevan ``LIMIT 100000`` explícito: sin él el motor
  aplicaría el tope de seguridad SELECT_ROW_CAP (100 filas) y el full
  scan se detendría tras 101 coincidencias, lo cual NO mediría el costo
  real de barrer el rango completo.
- El full scan cuesta ~0.3 s por consulta sobre 100k registros, así que
  esa variante se mide con 10 consultas por selectividad en vez de 50
  (columna ``consultas`` del CSV).

Uso:  python benchmarks/exp3_rangos.py
Salida: tabla por consola + benchmarks/results/exp3_rangos.csv
"""

import csv
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from common import (  # noqa: E402
    RESULTS_DIR,
    VARIANT_LABEL,
    bulk_load_timed,
    create_table,
    gen_rows,
    make_engine,
)

N = 100000
SELECTIVITIES = [0.1, 1, 5, 10, 25]  # % del total de tuplas
VARIANTS = ("heap", "heap_btree", "sequential")
QUERIES = 50
SCAN_QUERIES = 10  # full scan ~0.3 s/consulta sobre 100k registros


def run_cell(engine, variant: str, size: int) -> dict:
    n_queries = SCAN_QUERIES if variant == "heap" else QUERIES
    rng = random.Random(2024)  # mismas posiciones en todas las variantes
    reads, ms = [], []
    for _ in range(n_queries):
        a = rng.randint(0, N - size)
        b = a + size - 1
        # LIMIT explícito: sin tope de seguridad, el rango se barre entero.
        r = engine.execute(
            f"SELECT * FROM t WHERE id BETWEEN {a} AND {b} LIMIT {N};")
        assert r["ok"] and r["rowcount"] == size, r.get("error")
        reads.append(r["io"]["reads"])
        ms.append(r["elapsed_ms"])
    return {
        "consultas": n_queries,
        "reads_avg": round(statistics.mean(reads), 2),
        "reads_std": round(statistics.pstdev(reads), 2),
        "ms_avg": round(statistics.mean(ms), 3),
        "ms_std": round(statistics.pstdev(ms), 3),
    }


def run_variant(variant: str) -> list[dict]:
    engine = make_engine()
    create_table(engine, variant)
    rows = gen_rows(N, ordered=(variant == "sequential"))
    bulk_load_timed(engine, "t", rows)
    if variant == "sequential":
        engine.reorganize("t")  # área principal ordenada (ver docstring)
    out = []
    for pct in SELECTIVITIES:
        size = max(1, int(N * pct / 100))
        cell = run_cell(engine, variant, size)
        cell.update({
            "organizacion": VARIANT_LABEL[variant],
            "selectividad_pct": pct,
            "tuplas_rango": size,
        })
        out.append(cell)
        print(f"  {VARIANT_LABEL[variant]:<20} {pct:>5}% "
              f"({size:>6} tuplas)  reads={cell['reads_avg']:>8} ± "
              f"{cell['reads_std']:<7} ms={cell['ms_avg']:>8} ± "
              f"{cell['ms_std']:<8} ({cell['consultas']} consultas)")
    return out


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"Experimento 3: búsquedas por rango, selectividad variable (N={N})")
    rows = []
    for variant in VARIANTS:
        rows.extend(run_variant(variant))
    path = os.path.join(RESULTS_DIR, "exp3_rangos.csv")
    cols = ["organizacion", "selectividad_pct", "tuplas_rango", "consultas",
            "reads_avg", "reads_std", "ms_avg", "ms_std"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")

    print(f"\n{'Organización':<20} {'Sel.%':>6} {'Tuplas':>7} "
          f"{'Reads avg':>10} {'Reads std':>10} {'ms avg':>9} {'ms std':>9}")
    print("-" * 76)
    for r in rows:
        print(f"{r['organizacion']:<20} {r['selectividad_pct']:>6} "
              f"{r['tuplas_rango']:>7} {r['reads_avg']:>10} "
              f"{r['reads_std']:>10} {r['ms_avg']:>9} {r['ms_std']:>9}")


if __name__ == "__main__":
    main()
