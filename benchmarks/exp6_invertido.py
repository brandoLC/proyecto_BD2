"""Experimento 6: índice invertido en disco vs. Full Text Scan vs. GIN
(PostgreSQL).

Parte A — escalabilidad de la construcción (MiniDB):

- Corpus sintético determinístico de N ∈ [1 000, 2 000, 5 000, 10 000,
  20 000] documentos (vocabulario Zipf de 3 000 términos, documentos de
  20–60 palabras, semilla fija).
- Por cada N: tiempo de construcción del índice INVERTED y tamaño en
  disco de sus archivos (.dic + .inv + .docs + .nrm).

Parte B — latencia de consulta:

- Consultas booleanas (AND, OR, AND-NOT) y Top-K con COSINE_SCORE de
  1, 3 y 5 términos (términos de frecuencia media del corpus).
- Variantes: ``inverted`` (índice propio en disco), ``scan`` (Full Text
  Scan del motor, misma tabla sin índice) y — opcional — ``gin``
  (PostgreSQL con índice GIN sobre to_tsvector('spanish', texto)).
- Métricas: latencia (ms) promedio ± std sobre 20 consultas; lecturas de
  bloques (MiniDB: DiskCounter; PostgreSQL: shared read blocks de
  EXPLAIN (ANALYZE, BUFFERS)).

Uso:  python benchmarks/exp6_invertido.py [--with-postgres]
      [--pg-dsn "host=localhost port=5433 dbname=multimedia_db
                 user=postgres password=123456"]
Salida: tabla por consola + benchmarks/results/exp6_construccion.csv
        y benchmarks/results/exp6_consultas.csv
"""

import argparse
import csv
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from common import RESULTS_DIR, bulk_load_timed, make_engine  # noqa: E402

CORPUS_SIZES = (1000, 2000, 5000, 10000, 20000)
VOCAB_SIZE = 3000
QUERIES = 20
TERM_COUNTS = (1, 3, 5)
INDEX_EXTS = ("dic", "inv", "docs", "nrm")

# El Full Text Scan es O(N) en Python puro (~0.5 ms/documento): a corpus
# grande se miden pocas consultas y se reporta su número en el CSV.
SCAN_ITERS = {1000: 20, 2000: 20, 5000: 4, 10000: 2, 20000: 1}

PG_DSN_DEFAULT = ("host=localhost port=5433 dbname=multimedia_db "
                  "user=postgres password=123456")


# ----------------------------------------------------------------------
# Corpus sintético (Zipf sobre un vocabulario de términos "españoles")
# ----------------------------------------------------------------------
def make_vocab() -> list[str]:
    """3 000 términos pseudo-españoles, determinísticos."""
    rng = random.Random(99)
    silabas = ("la el ca co que de en io na ro es ta ra do re ci ma te "
               "lo ri al un da di go so ni or za").split()
    vocab = set()
    while len(vocab) < VOCAB_SIZE:
        n = rng.randint(2, 5)
        vocab.add("".join(rng.choice(silabas) for _ in range(n)))
    return sorted(vocab)


def make_corpus(n: int, vocab: list[str]) -> list[str]:
    """N documentos de 20–60 términos con distribución Zipf (1/rango)."""
    rng = random.Random(2024)
    ranks = list(range(1, len(vocab) + 1))
    weights = [1.0 / r for r in ranks]
    docs = []
    for _ in range(n):
        length = rng.randint(20, 60)
        picks = rng.choices(vocab, weights=weights, k=length)
        docs.append(" ".join(picks))
    return docs


def pick_query_terms(vocab: list[str]) -> list[str]:
    """Términos de frecuencia media (rango 100–600): selectividad útil."""
    rng = random.Random(7)
    lo, hi = 100, 600
    return rng.sample(vocab[lo:hi], QUERIES * sum(TERM_COUNTS))


# ----------------------------------------------------------------------
# Parte A + MiniDB de la parte B
# ----------------------------------------------------------------------
def index_files_size(engine, table: str) -> int:
    """Bytes totales de los archivos del índice INVERTED de la tabla."""
    total = 0
    for ext in INDEX_EXTS:
        path = os.path.join(engine.data_dir, f"{table}_texto.{ext}")
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return total


def run_minidb_queries(engine, table: str, terms: list[str],
                       label: str, n_queries: int = QUERIES,
                       term_offset: int = 0) -> list[dict]:
    """Consultas booleanas y Top-K de 1/3/5 términos sobre la tabla.

    ``term_offset`` permite que dos variantes (scan e inverted) usen las
    mismas consultas consumiendo distintas ventanas de ``terms``.
    """
    out = []
    pos = term_offset
    for n_terms in TERM_COUNTS:
        boolean_ms, boolean_reads, topk_ms, topk_reads = [], [], [], []
        for _ in range(n_queries):
            ts = terms[pos:pos + n_terms]
            pos += n_terms
            and_q = " AND ".join(ts)
            or_q = " OR ".join(ts)
            not_q = f"{ts[0]} AND NOT {ts[-1]}" if n_terms > 1 else ts[0]
            for sql in (
                f"SELECT id FROM {table} WHERE texto MATCH '{and_q}' "
                f"LIMIT 100000;",
                f"SELECT id FROM {table} WHERE texto MATCH '{or_q}' "
                f"LIMIT 100000;",
                f"SELECT id FROM {table} WHERE texto MATCH '{not_q}' "
                f"LIMIT 100000;",
            ):
                r = engine.execute(sql)
                assert r["ok"], r.get("error")
                boolean_ms.append(r["elapsed_ms"])
                boolean_reads.append(r["io"]["reads"])
            r = engine.execute(
                f"SELECT id, COSINE_SCORE() AS score FROM {table} "
                f"WHERE texto MATCH '{' '.join(ts)}' "
                f"ORDER BY score DESC LIMIT 10;")
            assert r["ok"], r.get("error")
            topk_ms.append(r["elapsed_ms"])
            topk_reads.append(r["io"]["reads"])
        out.append({
            "variante": label, "terminos": n_terms,
            "consultas": n_queries,
            "bool_ms_avg": round(statistics.mean(boolean_ms), 3),
            "bool_ms_std": round(statistics.pstdev(boolean_ms), 3),
            "bool_reads_avg": round(statistics.mean(boolean_reads), 2),
            "topk_ms_avg": round(statistics.mean(topk_ms), 3),
            "topk_ms_std": round(statistics.pstdev(topk_ms), 3),
            "topk_reads_avg": round(statistics.mean(topk_reads), 2),
        })
    return out


def run_minidb(n: int, docs: list[str], vocab: list[str],
               terms: list[str]) -> tuple[dict, list[dict]]:
    """Construcción (tiempo + tamaño) y consultas scan/inverted para N."""
    engine = make_engine()
    table = f"docs{n}"
    engine.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, "
                   f"texto TEXT);")
    rows = [(i + 2, [str(i + 1), doc]) for i, doc in enumerate(docs)]
    bulk_load_timed(engine, table, rows)

    # Full Text Scan (sin índice): línea base del motor. Es O(N) en
    # Python puro, así que a corpus grande se miden pocas consultas.
    scan_rows = run_minidb_queries(engine, table, terms, "scan",
                                   n_queries=SCAN_ITERS[n])

    t0 = time.perf_counter()
    engine.execute(
        f"CREATE INDEX ON {table} (texto) USING INVERTED "
        f"WITH (language = 'spanish');")
    build_ms = (time.perf_counter() - t0) * 1000
    size_bytes = index_files_size(engine, table)

    inv_rows = run_minidb_queries(engine, table, terms, "inverted")
    build = {
        "n": n,
        "build_ms": round(build_ms, 1),
        "size_kb": round(size_bytes / 1024, 1),
        "vocab_docs": len(docs),
    }
    print(f"  N={n:<6} build={build_ms:>9.1f} ms  "
          f"size={build['size_kb']:>9} KB")
    for r in scan_rows + inv_rows:
        r["n"] = n
    return build, scan_rows + inv_rows


# ----------------------------------------------------------------------
# PostgreSQL GIN (opcional)
# ----------------------------------------------------------------------
def run_postgres(dsn: str, n: int, docs: list[str],
                 terms: list[str]) -> list[dict]:
    """Misma batería contra PostgreSQL con índice GIN (tsvector)."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    table = f"minidb_exp6_{n}"
    cur.execute(f"DROP TABLE IF EXISTS {table};")
    cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, texto TEXT);")
    cur.executemany(f"INSERT INTO {table} VALUES (%s, %s);",
                    [(i + 1, d) for i, d in enumerate(docs)])
    t0 = time.perf_counter()
    cur.execute(
        f"CREATE INDEX {table}_gin ON {table} "
        f"USING GIN (to_tsvector('spanish', texto));")
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"  [postgres] N={n}: GIN construido en {build_ms:.1f} ms")

    def bench(sql: str, n_queries: int) -> tuple[float, float]:
        """Latencia media y bloques leídos (EXPLAIN ANALYZE BUFFERS)."""
        ms, reads = [], []
        for _ in range(n_queries):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
            plan = cur.fetchone()[0][0]
            ms.append(plan["Execution Time"])
            node = plan["Plan"]
            stack = [node]
            blocks = 0
            while stack:
                nd = stack.pop()
                blocks += nd.get("Shared Read Blocks", 0)
                stack.extend(nd.get("Plans", []))
            reads.append(blocks)
        return statistics.mean(ms), statistics.mean(reads)

    out = []
    pos = 0
    for n_terms in TERM_COUNTS:
        bool_ms, bool_reads, topk_ms, topk_reads = [], [], [], []
        for _ in range(QUERIES):
            ts = terms[pos:pos + n_terms]
            pos += n_terms
            and_q = " & ".join(ts)
            or_q = " | ".join(ts)
            not_q = f"{ts[0]} & !{ts[-1]}" if n_terms > 1 else ts[0]
            for q in (and_q, or_q, not_q):
                m, rd = bench(
                    f"SELECT id FROM {table} WHERE to_tsvector('spanish', "
                    f"texto) @@ to_tsquery('spanish', '{q}');", 1)
                bool_ms.append(m)
                bool_reads.append(rd)
            m, rd = bench(
                f"SELECT id, ts_rank(to_tsvector('spanish', texto), "
                f"to_tsquery('spanish', '{and_q}')) AS rank FROM {table} "
                f"WHERE to_tsvector('spanish', texto) @@ "
                f"to_tsquery('spanish', '{and_q}') "
                f"ORDER BY rank DESC LIMIT 10;", 1)
            topk_ms.append(m)
            topk_reads.append(rd)
        out.append({
            "variante": "gin", "terminos": n_terms, "consultas": QUERIES,
            "bool_ms_avg": round(statistics.mean(bool_ms), 3),
            "bool_ms_std": round(statistics.pstdev(bool_ms), 3),
            "bool_reads_avg": round(statistics.mean(bool_reads), 2),
            "topk_ms_avg": round(statistics.mean(topk_ms), 3),
            "topk_ms_std": round(statistics.pstdev(topk_ms), 3),
            "topk_reads_avg": round(statistics.mean(topk_reads), 2),
            "n": n,
        })
    cur.execute(f"DROP TABLE {table};")
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-postgres", action="store_true",
                    help="incluir la comparación con GIN de PostgreSQL")
    ap.add_argument("--pg-dsn", default=PG_DSN_DEFAULT)
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    vocab = make_vocab()
    terms = pick_query_terms(vocab)

    builds, queries = [], []
    for n in CORPUS_SIZES:
        docs = make_corpus(n, vocab)
        build, rows = run_minidb(n, docs, vocab, terms)
        builds.append(build)
        queries.extend(rows)
        if args.with_postgres:
            try:
                queries.extend(run_postgres(args.pg_dsn, n, docs, terms))
            except Exception as exc:
                print(f"  [postgres] N={n} omitido: {exc}")

    path = os.path.join(RESULTS_DIR, "exp6_construccion.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["n", "build_ms", "size_kb", "vocab_docs"])
        writer.writeheader()
        writer.writerows(builds)
    print(f"\nCSV guardado en {path}")

    path = os.path.join(RESULTS_DIR, "exp6_consultas.csv")
    cols = ["n", "variante", "terminos", "consultas",
            "bool_ms_avg", "bool_ms_std", "bool_reads_avg",
            "topk_ms_avg", "topk_ms_std", "topk_reads_avg"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(queries)
    print(f"CSV guardado en {path}")

    print(f"\n{'N':>6} {'variante':<10} {'térms':>6} {'bool ms':>10} "
          f"{'bool reads':>11} {'topk ms':>10} {'topk reads':>11}")
    print("-" * 70)
    for r in queries:
        print(f"{r['n']:>6} {r['variante']:<10} {r['terminos']:>6} "
              f"{r['bool_ms_avg']:>10} {r['bool_reads_avg']:>11} "
              f"{r['topk_ms_avg']:>10} {r['topk_reads_avg']:>11}")


if __name__ == "__main__":
    main()
