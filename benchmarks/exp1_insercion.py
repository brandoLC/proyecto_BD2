"""Experimento 1: inserción masiva por organización de archivo.

Inserta N registros ``(id INT PK, nombre VARCHAR(30), valor FLOAT)`` en:

- ``heap``:        tabla heap sin índice en la PK,
- ``heap_btree``:  tabla heap con B+ Tree en la PK,
- ``heap_hash``:   tabla heap con Hash Extensible en la PK,
- ``sequential``:  Sequential File (área ordenada + overflow).

Metodología (honesta respecto a qué se mide):

- La inserción se hace en LOTE con ``bulk_load_rows`` — el mismo camino
  físico de la carga CSV del motor (archivos abiertos una sola vez,
  volcado diferido de cabeceras y páginas) — no con INSERTs tupla a
  tupla, que medirían sobre todo el costo de abrir/cerrar archivos por
  fila. La I/O física se cuenta activando el DiskCounter alrededor de
  la llamada, igual que hace ``execute()`` internamente.
- Las variantes heap reciben las claves BARAJADAS (semilla fija). La
  variante sequential recibe las claves ORDENADAS por la PK: con 100k+
  tuplas desordenadas la inserción en overflow es O(N) por tupla (la
  cadena se recorre desde el predecesor); con datos ordenados aplica el
  camino rápido O(1) del tail. La inserción desordenada en sequential
  queda para el informe con N menores.

Uso:  python benchmarks/exp1_insercion.py
Salida: tabla por consola + benchmarks/results/exp1_insercion.csv
"""

import csv
import os
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

NS = [1000, 10000, 50000, 100000]
# NS += [250000, 500000]  # opcionales: demoran varios minutos por variante


def run() -> list[dict]:
    out = []
    for n in NS:
        for variant in VARIANTS:
            engine = make_engine()
            create_table(engine, variant)
            # sequential: carga ordenada por PK (camino rápido del tail).
            rows = gen_rows(n, ordered=(variant == "sequential"))
            elapsed, io = bulk_load_timed(engine, "t", rows)
            row = {
                "organizacion": VARIANT_LABEL[variant],
                "n": n,
                "tiempo_s": round(elapsed, 3),
                "disk_writes": io["writes"],
                "disk_reads": io["reads"],
                "rows_per_sec": round(n / elapsed),
            }
            out.append(row)
            print(f"  {VARIANT_LABEL[variant]:<22} n={n:<7} "
                  f"{row['tiempo_s']:>8} s  writes={row['disk_writes']:>8}  "
                  f"{row['rows_per_sec']:>9} filas/s")
    return out


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("Experimento 1: inserción masiva (bulk_load_rows)")
    rows = run()
    path = os.path.join(RESULTS_DIR, "exp1_insercion.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")

    print(f"\n{'Organización':<22} {'N':>7} {'Tiempo (s)':>11} "
          f"{'Writes':>9} {'Reads':>7} {'Filas/s':>10}")
    print("-" * 70)
    for r in rows:
        print(f"{r['organizacion']:<22} {r['n']:>7} {r['tiempo_s']:>11} "
              f"{r['disk_writes']:>9} {r['disk_reads']:>7} "
              f"{r['rows_per_sec']:>10}")


if __name__ == "__main__":
    main()
