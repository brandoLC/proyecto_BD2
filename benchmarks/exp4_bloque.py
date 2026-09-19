"""Experimento 4: sensibilidad del B+ Tree al tamaño de bloque.

Construye un B+ Tree con N = 100 000 claves INT para cada tamaño de
página B en [1024, 2048, 4096, 8192] bytes (parámetro ``page_size`` de
``BPlusTree``) y mide:

- ``leaf_cap`` / ``internal_cap``: entradas máximas por nodo hoja e
  interno según el formato de nodo (``fan-out`` = ``internal_cap + 1``
  hijos). Se derivan de la página: hoja = (B - 7) // (key + RID),
  interno = (B - 7) // (key + RID + 4).
- ``altura``: niveles del camino raíz -> hoja (``BPlusTree.height()``).
- ``reads_avg``: I/Os de búsquedas puntuales con la caché de nodos
  vaciada antes de cada consulta (peor caso: cada nivel = 1 página
  leída), sobre 1000 claves aleatorias existentes (semilla fija).
- ``paginas`` / ``bytes``: tamaño del archivo del índice.

La carga usa claves barajadas (semilla fija) con volcado diferido
(``defer_header``/``defer_flush``), el mismo camino de la carga masiva.

Uso:  python benchmarks/exp4_bloque.py
Salida: tabla por consola + benchmarks/results/exp4_bloque.csv
"""

import csv
import os
import random
import statistics
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.indexes.btree import BPlusTree  # noqa: E402
from app.storage.disk_counter import DiskCounter  # noqa: E402
from common import RESULTS_DIR  # noqa: E402

N = 100000
PAGE_SIZES = (1024, 2048, 4096, 8192)
QUERIES = 1000

ENC = lambda v: struct.pack("<i", v)  # noqa: E731
DEC = lambda b: struct.unpack("<i", b)[0]  # noqa: E731


def run_size(page_size: int, keys: list[int]) -> dict:
    path = os.path.join(tempfile.mkdtemp(prefix="minidb_exp4_"), "t.btree")
    counter = DiskCounter()
    with BPlusTree(path, 4, ENC, DEC, create=True,
                   counter=counter, page_size=page_size) as t:
        # Carga masiva: cabecera y páginas se vuelcan una sola vez.
        t.defer_header = True
        t.defer_flush = True
        t0 = time.perf_counter()
        for i, k in enumerate(keys):
            t.insert(k, (i, 0))
        t.flush_pages()
        t.flush_header()
        load_s = time.perf_counter() - t0

        height = t.height()
        pages = t.page_count

        # Búsquedas puntuales con caché fría: cada nivel leído cuenta.
        sample = random.Random(42).sample(keys, QUERIES)
        reads = []
        for k in sample:
            t._cache.clear()
            counter.reset()
            assert t.search(k) != []
            reads.append(counter.reads)

    return {
        "page_size": page_size,
        "leaf_cap": t.leaf_cap,
        "internal_cap": t.internal_cap,
        "altura": height,
        "paginas": pages,
        "bytes": pages * page_size,
        "carga_s": round(load_s, 3),
        "reads_avg": round(statistics.mean(reads), 2),
        "reads_std": round(statistics.pstdev(reads), 2),
    }


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print(f"Experimento 4: sensibilidad al tamaño de bloque (N={N})")
    keys = list(range(N))
    random.Random(1234).shuffle(keys)
    rows = []
    for b in PAGE_SIZES:
        row = run_size(b, keys)
        rows.append(row)
        print(f"  B={row['page_size']:>5}  hoja={row['leaf_cap']:>4}  "
              f"interno={row['internal_cap']:>4}  altura={row['altura']}  "
              f"páginas={row['paginas']:>6}  "
              f"reads={row['reads_avg']:>5} ± {row['reads_std']:<5} "
              f"carga={row['carga_s']:>7}s")
    path = os.path.join(RESULTS_DIR, "exp4_bloque.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")

    print(f"\n{'B (bytes)':>9} {'Hoja cap':>9} {'Interno cap':>12} "
          f"{'Altura':>7} {'Páginas':>8} {'MiB':>7} {'Reads avg':>10}")
    print("-" * 68)
    for r in rows:
        print(f"{r['page_size']:>9} {r['leaf_cap']:>9} "
              f"{r['internal_cap']:>12} {r['altura']:>7} {r['paginas']:>8} "
              f"{r['bytes'] / 2**20:>7.2f} {r['reads_avg']:>10}")


if __name__ == "__main__":
    main()
