"""Experimento 7: paradigma clásico (BoVW) vs. moderno (embeddings).

Sobre la tabla ``productos`` preparada por
``backend/scripts/prepare_recommender.py`` (Natural Images: 600
productos en 6 categorías, que sirven de verdad terrenal).

Por cada consulta (10 productos por categoría, semilla fija):

- **BoVW clásico**: extracción SIFT + asignación al codebook (tiempo de
  extracción) -> consulta ``bovw MATCH ... COSINE_SCORE()`` sobre el
  índice invertido (tiempo de consulta, lecturas de bloques).
- **Embeddings densos**: extracción ResNet-18 (tiempo de extracción)
  -> consulta ``embedding <=> ...`` sobre el índice IVF (tiempo de
  consulta, lecturas de bloques).

Efectividad semántica: Precision@5 y Precision@10, donde un resultado es
relevante si comparte la categoría de la consulta (se excluye el propio
producto consulta del ranking).

Uso:  python benchmarks/exp7_bovw_vs_embeddings.py [--data-dir DIR]
Salida: tabla por consola + benchmarks/results/exp7_bovw_vs_embeddings.csv
"""

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from common import RESULTS_DIR  # noqa: E402

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "..",
                                "backend", "data")
QUERIES_PER_CATEGORY = 10
TOP_K = 10


def load_engine(data_dir: str):
    os.environ["DATA_DIR"] = data_dir
    from app.ai import features, recommender  # noqa: PLC0415
    from app.engine.executor import Engine  # noqa: PLC0415
    cfg = recommender.load_config()
    if cfg is None:
        raise SystemExit("no existe app_config.json: ejecutar primero "
                         "backend/scripts/prepare_recommender.py")
    return Engine(data_dir), features, cfg


def product_rows(engine, table: str) -> list[dict]:
    """(id, categoria, imagen) de todos los productos."""
    res = engine.execute(
        f"SELECT id, categoria, imagen FROM {table} LIMIT 100000;")
    assert res["ok"], res.get("error")
    return [dict(zip(res["columns"], row)) for row in res["rows"]]


def precision(retrieved_cats: list[str], query_cat: str, k: int) -> float:
    return sum(1 for c in retrieved_cats[:k] if c == query_cat) / k


def run(data_dir: str) -> list[dict]:
    engine, features, cfg = load_engine(data_dir)
    table = cfg["table"]
    rows = product_rows(engine, table)
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["categoria"], []).append(r)

    rng = random.Random(2024)
    queries = []
    for cat in sorted(by_cat):
        pool = by_cat[cat]
        queries.extend(rng.sample(pool, min(QUERIES_PER_CATEGORY,
                                            len(pool))))

    metrics = {m: {"p5": [], "p10": [], "extract_ms": [], "query_ms": [],
                   "reads": []} for m in ("bovw", "embedding")}
    for qi, q in enumerate(queries, 1):
        img_path = os.path.join(features.images_dir(), q["imagen"])
        with open(img_path, "rb") as f:
            image_bytes = f.read()

        # --- BoVW clásico ---
        t0 = time.perf_counter()
        words = features.image_visual_words(image_bytes)
        extract_ms = (time.perf_counter() - t0) * 1000
        match_q = " ".join(words).replace("'", "''")
        res = engine.execute(
            f"SELECT id, categoria, COSINE_SCORE() AS score FROM {table} "
            f"WHERE bovw MATCH '{match_q}' "
            f"ORDER BY score DESC LIMIT {TOP_K + 1};")
        assert res["ok"], res.get("error")
        cats = [r[1] for r in res["rows"] if r[0] != q["id"]]
        m = metrics["bovw"]
        m["extract_ms"].append(extract_ms)
        m["query_ms"].append(res["elapsed_ms"])
        m["reads"].append(res["io"]["reads"])
        m["p5"].append(precision(cats, q["categoria"], 5))
        m["p10"].append(precision(cats, q["categoria"], 10))

        # --- Embeddings densos (IVF) ---
        t0 = time.perf_counter()
        vec = features.image_embedding(image_bytes)
        extract_ms = (time.perf_counter() - t0) * 1000
        lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        res = engine.execute(
            f"SELECT id, categoria, embedding <=> '{lit}' AS dist "
            f"FROM {table} LIMIT {TOP_K + 1};")
        assert res["ok"], res.get("error")
        cats = [r[1] for r in res["rows"] if r[0] != q["id"]]
        m = metrics["embedding"]
        m["extract_ms"].append(extract_ms)
        m["query_ms"].append(res["elapsed_ms"])
        m["reads"].append(res["io"]["reads"])
        m["p5"].append(precision(cats, q["categoria"], 5))
        m["p10"].append(precision(cats, q["categoria"], 10))
        if qi % 10 == 0:
            print(f"  {qi}/{len(queries)} consultas...", flush=True)

    out = []
    for name, m in metrics.items():
        out.append({
            "paradigma": name,
            "consultas": len(queries),
            "precision_at_5": round(statistics.mean(m["p5"]), 4),
            "precision_at_10": round(statistics.mean(m["p10"]), 4),
            "extract_ms_avg": round(statistics.mean(m["extract_ms"]), 2),
            "query_ms_avg": round(statistics.mean(m["query_ms"]), 2),
            "query_ms_std": round(statistics.pstdev(m["query_ms"]), 2),
            "reads_avg": round(statistics.mean(m["reads"]), 2),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="DATA_DIR del backend con la tabla productos")
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("Experimento 7: BoVW (índice invertido) vs. embeddings (IVF)")
    rows = run(os.path.abspath(args.data_dir))
    path = os.path.join(RESULTS_DIR, "exp7_bovw_vs_embeddings.csv")
    cols = ["paradigma", "consultas", "precision_at_5", "precision_at_10",
            "extract_ms_avg", "query_ms_avg", "query_ms_std", "reads_avg"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV guardado en {path}")

    print(f"\n{'Paradigma':<11} {'P@5':>7} {'P@10':>7} {'extr. ms':>9} "
          f"{'query ms':>9} {'reads':>8}")
    print("-" * 56)
    for r in rows:
        print(f"{r['paradigma']:<11} {r['precision_at_5']:>7} "
              f"{r['precision_at_10']:>7} {r['extract_ms_avg']:>9} "
              f"{r['query_ms_avg']:>9} {r['reads_avg']:>8}")


if __name__ == "__main__":
    main()
