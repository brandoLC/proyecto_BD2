"""Experimento 8: compromiso eficiencia vs. calidad en búsqueda ANN (IVF).

Sobre la tabla ``productos`` preparada por
``backend/scripts/prepare_recommender.py``.

- **Verdad base (ground truth)**: Top-10 exacto por similitud coseno
  calculado con el k-NN lineal del propio motor — se crea una copia de la
  tabla SIN índice IVF para que el planificador use "Exact Vector Scan".
- **Curva**: ``probes`` ∈ [1, 2, 5, 10, 20, 50] sobre el índice IVF de
  ``productos`` (vía ``SET ivf.probes``). Por cada valor: Recall@10 medio
  (|ANN ∩ exacto| / 10 sobre 30 consultas aleatorias), latencia media y
  lecturas de bloques (DiskCounter del motor).

Uso:  python benchmarks/exp8_ann.py [--data-dir DIR]
Salida: tabla por consola + benchmarks/results/exp8_ann.csv
"""

import argparse
import csv
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from common import RESULTS_DIR  # noqa: E402

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "..",
                                "backend", "data")
PROBES = (1, 2, 5, 10, 20, 50)
QUERIES = 30
TOP_K = 10


def load(data_dir: str):
    os.environ["DATA_DIR"] = data_dir
    from app.ai import features, recommender  # noqa: PLC0415
    from app.engine.executor import Engine  # noqa: PLC0415
    cfg = recommender.load_config()
    if cfg is None:
        raise SystemExit("no existe app_config.json: ejecutar primero "
                         "backend/scripts/prepare_recommender.py")
    return Engine(data_dir), features, cfg


def make_exact_table(engine, table: str) -> str:
    """Copia ``productos`` -> ``productos_exact`` SIN índice IVF.

    El planificador resuelve la distancia vectorial sin índice con
    "Exact Vector Scan" (k-NN lineal), la verdad base del experimento.
    """
    exact = f"{table}_exact"
    engine.execute(f"DROP TABLE {exact};")  # por si quedó de una corrida
    res = engine.execute(
        f"CREATE TABLE {exact} (id INT PRIMARY KEY, "
        f"embedding VECTOR(512));")
    assert res["ok"], res.get("error")
    src = engine.execute(f"SELECT id, embedding FROM {table} "
                         f"LIMIT 100000;")
    assert src["ok"], src.get("error")
    rows = [(i + 2, [str(r[0]),
                     "[" + ",".join(f"{v:.6f}" for v in r[1]) + "]"])
            for i, r in enumerate(src["rows"])]
    stats = engine.bulk_load_rows(exact, rows)
    assert stats["rows_rejected"] == 0, stats["errors"][:3]
    return exact


def query_vectors(engine, features, table: str):
    """Vectores de consulta: embeddings de 30 productos al azar."""
    src = engine.execute(f"SELECT id, imagen FROM {table} LIMIT 100000;")
    assert src["ok"], src.get("error")
    picks = random.Random(2024).sample(
        [dict(zip(src["columns"], r)) for r in src["rows"]],
        min(QUERIES, len(src["rows"])))
    out = []
    for p in picks:
        with open(os.path.join(features.images_dir(), p["imagen"]),
                  "rb") as f:
            out.append((p["id"], features.image_embedding(f.read())))
    return out


def run(data_dir: str) -> list[dict]:
    engine, features, cfg = load(data_dir)
    table = cfg["table"]
    exact_table = make_exact_table(engine, table)
    queries = query_vectors(engine, features, table)

    # Ground truth: Top-10 exacto (k-NN lineal, sin índice).
    ground_truth: dict[int, list[int]] = {}
    exact_ms = []
    for qid, vec in queries:
        lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        res = engine.execute(
            f"SELECT id, embedding <=> '{lit}' AS dist "
            f"FROM {exact_table} LIMIT {TOP_K};")
        assert res["ok"], res.get("error")
        ground_truth[qid] = [r[0] for r in res["rows"]]
        exact_ms.append(res["elapsed_ms"])
    print(f"  ground truth exacto: {statistics.mean(exact_ms):.2f} ms "
          f"promedio por consulta")

    out = []
    for probes in PROBES:
        engine.execute(f"SET ivf.probes = {probes};")
        recalls, ms, reads = [], [], []
        for qid, vec in queries:
            lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            res = engine.execute(
                f"SELECT id, embedding <=> '{lit}' AS dist "
                f"FROM {table} LIMIT {TOP_K};")
            assert res["ok"], res.get("error")
            ann = {r[0] for r in res["rows"]}
            recalls.append(len(ann & set(ground_truth[qid])) / TOP_K)
            ms.append(res["elapsed_ms"])
            reads.append(res["io"]["reads"])
        row = {
            "probes": probes,
            "recall_at_10": round(statistics.mean(recalls), 4),
            "recall_std": round(statistics.pstdev(recalls), 4),
            "ms_avg": round(statistics.mean(ms), 3),
            "ms_std": round(statistics.pstdev(ms), 3),
            "reads_avg": round(statistics.mean(reads), 2),
            "reads_std": round(statistics.pstdev(reads), 2),
        }
        out.append(row)
        print(f"  probes={probes:<3} recall@10={row['recall_at_10']:.3f} "
              f"ms={row['ms_avg']:>8} reads={row['reads_avg']:>7}")

    engine.execute(f"DROP TABLE {exact_table};")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="DATA_DIR del backend con la tabla productos")
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("Experimento 8: Recall@10 vs. probes (IVF) "
          f"({QUERIES} consultas, Top-{TOP_K})")
    rows = run(os.path.abspath(args.data_dir))
    path = os.path.join(RESULTS_DIR, "exp8_ann.csv")
    cols = ["probes", "recall_at_10", "recall_std", "ms_avg", "ms_std",
            "reads_avg", "reads_std"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")


if __name__ == "__main__":
    main()
