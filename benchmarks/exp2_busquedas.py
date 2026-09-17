"""Experimento 2: búsquedas puntuales de igualdad sobre la PK.

Sobre tablas de N = 100 000 registros (misma precarga por organización
que el Experimento 1) ejecuta búsquedas ``SELECT * FROM t WHERE id = k``
con claves aleatorias existentes (semilla fija) y mide por consulta la
I/O física (``io.reads`` del DiskCounter) y la latencia (``elapsed_ms``):

- ``heap``:        full scan (sin índice sobre la PK),
- ``heap_btree``:  Index Scan con B+ Tree,
- ``heap_hash``:   Index Scan con Hash Extensible,
- ``sequential``:  búsqueda binaria del Sequential File.

Notas de metodología:

- La variante sequential se REORGANIZA tras la carga (operación
  periódica normal de esta organización): las búsquedas binarias se
  miden sobre el área principal ordenada, no sobre una cadena de
  overflow de 100k tuplas.
- El full scan sobre 100k registros cuesta ~0.3 s por consulta (lee y
  deserializa la tabla completa), así que esa variante se mide con 100
  consultas en vez de 1000 (columna ``consultas`` del CSV); las demás
  usan 1000.

Uso:  python benchmarks/exp2_busquedas.py
Salida: tabla por consola + benchmarks/results/exp2_busquedas.csv
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
    VARIANTS,
    bulk_load_timed,
    create_table,
    gen_rows,
    make_engine,
)

N = 100000
QUERIES = 1000
SCAN_QUERIES = 100  # full scan ~0.3 s/consulta sobre 100k registros


def run_variant(variant: str) -> dict:
    engine = make_engine()
    create_table(engine, variant)
    rows = gen_rows(N, ordered=(variant == "sequential"))
    bulk_load_timed(engine, "t", rows)
    if variant == "sequential":
        engine.reorganize("t")  # área principal ordenada (ver docstring)

    n_queries = SCAN_QUERIES if variant == "heap" else QUERIES
    keys = random.Random(42).sample(range(N), n_queries)
    reads, ms = [], []
    for k in keys:
        r = engine.execute(f"SELECT * FROM t WHERE id = {k};")
        assert r["ok"] and r["rowcount"] == 1, r
        reads.append(r["io"]["reads"])
        ms.append(r["elapsed_ms"])
    return {
        "organizacion": VARIANT_LABEL[variant],
        "n": N,
        "consultas": n_queries,
        "reads_avg": round(statistics.mean(reads), 2),
        "reads_std": round(statistics.pstdev(reads), 2),
        "ms_avg": round(statistics.mean(ms), 3),
        "ms_std": round(statistics.pstdev(ms), 3),
    }


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"Experimento 2: búsquedas puntuales de igualdad (N={N})")
    rows = []
    for variant in VARIANTS:
        row = run_variant(variant)
        rows.append(row)
        print(f"  {row['organizacion']:<22} "
              f"reads={row['reads_avg']:>8} ± {row['reads_std']:<7} "
              f"ms={row['ms_avg']:>8} ± {row['ms_std']:<8} "
              f"({row['consultas']} consultas)")
    path = os.path.join(RESULTS_DIR, "exp2_busquedas.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")

    print(f"\n{'Organización':<22} {'Consultas':>9} {'Reads avg':>10} "
          f"{'Reads std':>10} {'ms avg':>9} {'ms std':>9}")
    print("-" * 72)
    for r in rows:
        print(f"{r['organizacion']:<22} {r['consultas']:>9} "
              f"{r['reads_avg']:>10} {r['reads_std']:>10} "
              f"{r['ms_avg']:>9} {r['ms_std']:>9}")


if __name__ == "__main__":
    main()
