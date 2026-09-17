"""Utilidades compartidas de los benchmarks (Fase 6).

Importa el motor directamente desde ``backend/`` y crea instancias
aisladas de ``Engine`` en directorios temporales, igual que los tests
(``backend/tests/test_executor.py``).
"""

import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.engine.executor import Engine  # noqa: E402
from app.storage.disk_counter import DiskCounter  # noqa: E402

SCHEMA = "(id INT PRIMARY KEY, nombre VARCHAR(30), valor FLOAT)"
VARIANTS = ("heap", "heap_btree", "heap_hash", "sequential")

VARIANT_LABEL = {
    "heap": "Heap sin índice",
    "heap_btree": "Heap + B+ Tree (PK)",
    "heap_hash": "Heap + Hash Ext. (PK)",
    "sequential": "Sequential File",
}

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def make_engine() -> Engine:
    """Engine aislado en un directorio temporal nuevo."""
    data_dir = tempfile.mkdtemp(prefix="minidb_bench_")
    return Engine(os.path.join(data_dir, "data"))


def create_table(engine: Engine, variant: str, table: str = "t") -> None:
    """Crea la tabla con la organización/acceso de la variante.

    - ``heap``: tabla heap SIN índice en la PK (se retira el B+ Tree
      automático; ver ``_strip_pk_index``).
    - ``heap_btree``: tabla heap con el B+ Tree automático de la PK.
    - ``heap_hash``: tabla heap con Hash Extensible como ÚNICA vía de
      acceso de la PK (se reemplaza el B+ Tree automático).
    - ``sequential``: tabla con ``USING SEQUENTIAL``.
    """
    if variant == "sequential":
        engine.execute(f"CREATE TABLE {table} {SCHEMA} USING SEQUENTIAL;")
        return
    engine.execute(f"CREATE TABLE {table} {SCHEMA};")
    if variant == "heap":
        _strip_pk_index(engine, table)
    elif variant == "heap_hash":
        _strip_pk_index(engine, table)
        engine.execute(f"CREATE INDEX ON {table} (id) USING HASH;")


def _strip_pk_index(engine: Engine, table: str) -> None:
    """Elimina el B+ Tree automático de la PK (catálogo + archivo).

    El motor no tiene DROP INDEX; para medir "heap sin índice" y
    "heap + HASH como única vía" se retira a mano la entrada del
    catálogo y se borra el archivo del índice automático.
    """
    meta = engine.catalog.indexes(table)[0]
    path = engine._index_path(table, meta["column"], meta["type"])
    engine.catalog._data["tables"][table]["indexes"] = []
    engine.catalog.save()
    if os.path.isfile(path):
        os.remove(path)


def gen_rows(n: int, ordered: bool) -> list[tuple[int, list[str]]]:
    """``n`` filas crudas (como las de un CSV) para ``bulk_load_rows``.

    Con ``ordered=False`` las claves van barajadas (semilla fija para
    reproducibilidad); con ``ordered=True`` van ordenadas por la PK.
    """
    ids = list(range(n))
    if not ordered:
        random.Random(1234).shuffle(ids)
    return [(i + 2, [str(k), f"nombre-{k}", f"{k * 1.5:.2f}"])
            for i, k in enumerate(ids)]


def bulk_load_timed(engine: Engine, table: str,
                    rows: list[tuple[int, list[str]]]) -> tuple[float, dict]:
    """Carga masiva midiendo tiempo total e I/O física.

    ``bulk_load_rows`` es el mismo camino de la carga CSV (un solo
    archivo abierto por estructura, volcado diferido al final). Como no
    pasa por ``execute()``, el DiskCounter se activa manualmente
    alrededor de la llamada — exactamente lo que hace ``execute()``
    internamente — así las escrituras/lecturas de páginas del heap, de
    los índices y del sequential file quedan contabilizadas.
    """
    engine._counter = DiskCounter()
    t0 = time.perf_counter()
    try:
        stats = engine.bulk_load_rows(table, rows)
        elapsed = time.perf_counter() - t0
        io = engine._counter.snapshot()
    finally:
        engine._counter = None
    assert stats["rows_rejected"] == 0, stats["errors"][:3]
    return elapsed, io
