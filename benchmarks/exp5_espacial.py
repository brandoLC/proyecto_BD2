"""Experimento 5: escalabilidad espacial y maldición de la dimensionalidad.

Parte A — R-Tree vs. escaneo secuencial (motor MiniDB):

- N ∈ [10 000, 50 000, 100 000] puntos 2D uniformes en el área de Lima.
- Consultas de radio (r = 0.005° ≈ 0.55 km y r = 0.05° ≈ 5.5 km) y k-NN
  con k ∈ [5, 20, 50].
- Variantes: ``rtree`` (índice R-Tree en disco) y ``scan`` (heap sin
  índice, filtro por fuerza bruta). Métricas: ``io.reads`` y latencia
  (ms), promedio ± std sobre 30 consultas con semilla fija.

Parte B — maldición de la dimensionalidad (estudio independiente):

El R-Tree del motor es 2D (columnas POINT), así que el efecto de la
dimensión se evalúa con un R-Tree N-dimensional empaquetado (STR:
partición recursiva alternando dimensiones, hojas de 50 puntos). Es el
mejor caso posible de organización — cajas ajustadas y sin solapamiento
extra de inserciones dinámicas —: con N = 20 000 puntos uniformes en
[0,1]^D y D ∈ [2, 4, 8, 16, 32, 64, 128] se mide la fracción de hojas
(páginas de datos) cuyo MBR queda a mindist ≤ distancia al 10-ésimo
vecino, es decir las que CUALQUIER k-NN con poda por MBR debe leer. Al
crecer D esa fracción tiende al 100 % y el árbol degenera en escaneo
secuencial (un R-Tree dinámico degenera aún antes).

Uso:  python benchmarks/exp5_espacial.py
Salida: tabla por consola + benchmarks/results/exp5_espacial.csv
        y benchmarks/results/exp5_dimensionalidad.csv
"""

import csv
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from common import RESULTS_DIR, bulk_load_timed, make_engine  # noqa: E402

# ----------------------------------------------------------------------
# Parte A: R-Tree vs. secuencial en MiniDB
# ----------------------------------------------------------------------
SIZES = (10000, 50000, 100000)
K_VALUES = (5, 20, 50)
RADII = (0.005, 0.05)  # grados (~0.55 km y ~5.5 km)
QUERIES = 30
# Caja del área metropolitana de Lima (lat, lng)
LAT_LO, LAT_HI = -12.35, -11.75
LNG_LO, LNG_HI = -77.15, -76.75


def gen_points(n: int) -> list[tuple[int, list[str]]]:
    """N filas (id, nombre, valor, punto) con semilla fija."""
    rng = random.Random(777)
    return [(i + 2,
             [str(k), f"lugar-{k}", f"{k * 0.5:.2f}",
              f"({rng.uniform(LAT_LO, LAT_HI):.6f}, "
              f"{rng.uniform(LNG_LO, LNG_HI):.6f})"])
            for i, k in enumerate(range(1, n + 1))]


def build_table(n: int, with_rtree: bool):
    """Tabla de N puntos; con ``with_rtree=False`` se mide el full scan."""
    engine = make_engine()
    engine.execute(
        "CREATE TABLE geo (id INT PRIMARY KEY, nombre VARCHAR(30), "
        "valor FLOAT, ubicacion POINT);")
    bulk_load_timed(engine, "geo", gen_points(n))
    if with_rtree:
        engine.execute("CREATE INDEX ON geo (ubicacion) USING RTREE;")
    return engine


def run_spatial(engine, n: int, label: str) -> list[dict]:
    rng = random.Random(2024)  # mismas consultas para ambas variantes
    out = []
    specs = [("radio", None, r) for r in RADII] + [
        ("knn", k, None) for k in K_VALUES]
    for kind, k, radius in specs:
        reads, ms = [], []
        for _ in range(QUERIES):
            lat = rng.uniform(LAT_LO, LAT_HI)
            lng = rng.uniform(LNG_LO, LNG_HI)
            if kind == "radio":
                sql = (f"SELECT * FROM geo WHERE ubicacion IN "
                       f"(({lat:.6f}, {lng:.6f}), {radius}) LIMIT 100000;")
            else:
                sql = (f"SELECT * FROM geo WHERE ubicacion KNN "
                       f"(({lat:.6f}, {lng:.6f}), {k});")
            r = engine.execute(sql)
            assert r["ok"], r.get("error")
            reads.append(r["io"]["reads"])
            ms.append(r["elapsed_ms"])
        row = {
            "n": n, "variante": label, "consulta": kind,
            "k": k if k else "",
            "radio_grados": radius if kind == "radio" else "",
            "consultas": QUERIES,
            "reads_avg": round(statistics.mean(reads), 2),
            "reads_std": round(statistics.pstdev(reads), 2),
            "ms_avg": round(statistics.mean(ms), 3),
            "ms_std": round(statistics.pstdev(ms), 3),
        }
        out.append(row)
        tag = f"r={radius}" if kind == "radio" else f"knn{k}"
        print(f"  N={n:<7} {label:<10} {tag:<10} "
              f"reads={row['reads_avg']:>9} ± {row['reads_std']:<8} "
              f"ms={row['ms_avg']:>9} ± {row['ms_std']}")
    return out


# ----------------------------------------------------------------------
# Parte B: R-Tree N-dimensional en memoria (curse of dimensionality)
# ----------------------------------------------------------------------
DIMS = (2, 4, 8, 16, 32, 64, 128)
N_POINTS = 20000
MAX_ENTRIES = 50       # ~entradas por nodo (página)
KNN_K = 10
DIM_QUERIES = 20


def _union(a, b):
    return tuple((min(a[d][0], b[d][0]), max(a[d][1], b[d][1]))
                 for d in range(len(a)))


def _point_mbr(p):
    return tuple((x, x) for x in p)


def _mindist(mbr, q) -> float:
    s = 0.0
    for d, x in enumerate(q):
        lo, hi = mbr[d]
        if x < lo:
            s += (lo - x) ** 2
        elif x > hi:
            s += (x - hi) ** 2
    return math.sqrt(s)


def _dist2(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


class PackedRTree:
    """R-Tree N-dim empaquetado (STR): partición recursiva por dimensión.

    Es el mejor caso posible de organización (cajas ajustadas, sin
    underflow): si ni así la poda es selectiva en dimensión alta, un
    R-Tree dinámico (con solapamiento adicional) degenera aún antes.
    """

    def __init__(self, points, max_entries: int = MAX_ENTRIES) -> None:
        self.M = max_entries
        self.leaves = []  # lista de (mbr, [puntos])
        self._pack(list(points))

    def _pack(self, points, depth: int = 0) -> None:
        if len(points) <= self.M:
            mbr = None
            for p in points:
                pm = _point_mbr(p)
                mbr = pm if mbr is None else _union(mbr, pm)
            self.leaves.append((mbr, points))
            return
        d = depth % len(points[0])
        points.sort(key=lambda p: p[d])
        step = math.ceil(len(points) / math.ceil(len(points) / self.M))
        for i in range(0, len(points), step):
            self._pack(points[i:i + step], depth + 1)

    def knn_leaves_visited(self, q, k: int) -> int:
        """Hojas cuyo mindist a ``q`` es menor que la distancia al k-ésimo
        vecino exacto: las que cualquier k-NN con poda por MBR debe leer."""
        best = sorted(_dist2(q, p)
                      for _mbr, pts in self.leaves for p in pts)[k - 1]
        r = math.sqrt(best)
        return sum(1 for mbr, _pts in self.leaves
                   if _mindist(mbr, q) <= r)


def run_dimensionality() -> list[dict]:
    rng = random.Random(4242)
    out = []
    for d in DIMS:
        points = [[rng.random() for _ in range(d)]
                  for _ in range(N_POINTS)]
        tree = PackedRTree(points)
        queries = [[rng.random() for _ in range(d)]
                   for _ in range(DIM_QUERIES)]
        visited, ms = [], []
        for q in queries:
            t0 = time.perf_counter()
            visited.append(tree.knn_leaves_visited(q, KNN_K))
            ms.append((time.perf_counter() - t0) * 1000)
        total = len(tree.leaves)
        pct = 100.0 * statistics.mean(visited) / total
        row = {
            "d": d,
            "n": N_POINTS,
            "k": KNN_K,
            "hojas_total": total,
            "hojas_visitadas_avg": round(statistics.mean(visited), 2),
            "hojas_visitadas_std": round(statistics.pstdev(visited), 2),
            "pct_hojas_visitadas": round(pct, 2),
            "ms_avg": round(statistics.mean(ms), 2),
        }
        out.append(row)
        print(f"  D={d:<4} hojas={total:<5} visitadas="
              f"{row['hojas_visitadas_avg']:>7} "
              f"({row['pct_hojas_visitadas']:>6}%)")
    return out


def main() -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("Experimento 5A: R-Tree vs. escaneo secuencial")
    rows_a = []
    for n in SIZES:
        for label, with_rtree in (("scan", False), ("rtree", True)):
            engine = build_table(n, with_rtree)
            rows_a.extend(run_spatial(engine, n, label))
    path = os.path.join(RESULTS_DIR, "exp5_espacial.csv")
    cols = ["n", "variante", "consulta", "k", "radio_grados", "consultas",
            "reads_avg", "reads_std", "ms_avg", "ms_std"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows_a)
    print(f"CSV guardado en {path}")

    print("\nExperimento 5B: maldición de la dimensionalidad (R-Tree N-dim "
          "empaquetado)")
    rows_b = run_dimensionality()
    path = os.path.join(RESULTS_DIR, "exp5_dimensionalidad.csv")
    cols = ["d", "n", "k", "hojas_total", "hojas_visitadas_avg",
            "hojas_visitadas_std", "pct_hojas_visitadas", "ms_avg"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows_b)
    print(f"CSV guardado en {path}")


if __name__ == "__main__":
    main()
