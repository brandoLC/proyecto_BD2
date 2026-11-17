"""Prepara la tabla ``productos`` del recomendador e-commerce (Entregable 2).

Extrae las dos representaciones del Laboratorio 11 para cada imagen
(BoVW con SIFT + K-Means, y embedding ResNet-18 de 512D), genera los
metadatos comerciales sintéticos (precio, tienda con coordenadas en
Lima) y carga todo en MiniDB con los cuatro índices del proyecto:

- ``BTREE``  sobre ``precio``       (filtros relacionales)
- ``RTREE``  sobre ``tienda``       (búsqueda por proximidad de tienda)
- ``INVERTED`` sobre ``descripcion`` (búsqueda textual, español)
- ``INVERTED`` sobre ``bovw``        (palabras visuales, language='none')
- ``IVF``    sobre ``embedding``    (búsqueda ANN por similitud coseno)

Fuentes soportadas::

    # Natural Images (validación, 600 imágenes del Lab 11):
    .venv/bin/python scripts/prepare_recommender.py \\
        --source natural --data-dir /ruta/a/natural_images

    # Fashion Product Images de Kaggle (entrega, >= 10k productos):
    #   kaggle datasets download -d paramaggarwal/fashion-product-images-dataset
    .venv/bin/python scripts/prepare_recommender.py \\
        --source fashion --data-dir /ruta/a/fashion-dataset --limit 10000

Opciones: ``--limit N`` acota el número de productos; ``--reset`` borra
la tabla, los thumbnails y el codebook antes de regenerar todo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import random
import sys
import time

import numpy as np

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BACKEND_DIR, "data")

sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault("DATA_DIR", DATA_DIR)  # antes de importar app.ai

from app.ai import features  # noqa: E402
from app.engine.executor import Engine  # noqa: E402

TABLE = "productos"
EMB_DIM = 512
K_WORDS = 100

# Tiendas sintéticas (Lima) para la columna POINT indexada con R-Tree.
STORES = [
    ("Saga Miraflores", -12.1192, -77.0297),
    ("Ripley San Isidro", -12.0973, -77.0365),
    ("Oechsle Centro", -12.0464, -77.0428),
    ("H&M Jockey Plaza", -12.0858, -76.9774),
    ("Zara Larcomar", -12.1315, -77.0298),
    ("Falabella Megaplaza", -11.9936, -77.0607),
    ("Ripley Plaza Norte", -11.9893, -77.0583),
    ("Saga Salaverry", -12.0867, -77.0511),
    ("Oechsle San Miguel", -12.0774, -77.0816),
    ("H&M Real Plaza", -12.0563, -77.0371),
    ("Ripley Surco", -12.1462, -76.9859),
    ("Saga Chorrillos", -12.1733, -77.0110),
]

# Categorías de Natural Images -> nombre comercial en español.
NATURAL_ES = {
    "airplane": "avión",
    "car": "auto",
    "cat": "gato",
    "dog": "perro",
    "flower": "flor",
    "fruit": "fruta",
}

CREATE_TABLE_SQL = f"""
CREATE TABLE {TABLE} (
    id INT PRIMARY KEY,
    nombre VARCHAR(120),
    categoria VARCHAR(48),
    precio FLOAT,
    tienda_nombre VARCHAR(48),
    tienda POINT,
    descripcion TEXT,
    bovw TEXT,
    imagen VARCHAR(80),
    embedding VECTOR({EMB_DIM})
);
"""


# ----------------------------------------------------------------------
# Lectura de las fuentes
# ----------------------------------------------------------------------
def read_natural(data_dir: str, limit: int | None) -> list[dict]:
    """Natural Images: un subdirectorio por categoría con JPEGs."""
    items: list[dict] = []
    for cat in sorted(os.listdir(data_dir)):
        cat_dir = os.path.join(data_dir, cat)
        if not os.path.isdir(cat_dir):
            continue
        for fname in sorted(os.listdir(cat_dir)):
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                items.append({"category": cat,
                              "path": os.path.join(cat_dir, fname)})
    items = items[:limit] if limit else items
    for pid, item in enumerate(items, start=1):
        cat_es = NATURAL_ES.get(item["category"], item["category"])
        item.update({
            "id": pid,
            "nombre": f"{cat_es} #{pid}",
            "categoria": cat_es,
            "descripcion": (
                f"{cat_es} de exhibición, pieza original de la categoría "
                f"{cat_es}. Ideal para colección y decoración del hogar. "
                f"Producto nuevo con garantía de la tienda."),
        })
    return items


def read_fashion(data_dir: str, limit: int | None) -> list[dict]:
    """Fashion Product Images (Kaggle): ``styles.csv`` + ``images/``."""
    styles_path = os.path.join(data_dir, "styles.csv")
    images_dir = os.path.join(data_dir, "images")
    items: list[dict] = []
    with open(styles_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                pid = int(row["id"])
            except (KeyError, ValueError):
                continue
            img_path = os.path.join(images_dir, f"{pid}.jpg")
            if not os.path.exists(img_path):
                continue
            nombre = (row.get("productDisplayName") or "").strip()[:100]
            if not nombre:
                continue
            cat = (row.get("masterCategory") or "").strip() or "General"
            desc = (
                f"{nombre}. {row.get('articleType', '')} "
                f"{row.get('baseColour', '')} para {row.get('gender', '')}, "
                f"temporada {row.get('season', '')} {row.get('year', '')}. "
                f"Uso: {row.get('usage', '')}.").replace("  ", " ")
            items.append({"id": pid, "category": cat, "path": img_path,
                          "nombre": nombre, "categoria": cat,
                          "descripcion": desc})
            if limit and len(items) >= limit:
                break
    return items


# ----------------------------------------------------------------------
# Metadatos sintéticos determinísticos
# ----------------------------------------------------------------------
def add_metadata(items: list[dict]) -> None:
    """Precio y tienda sintéticos, determinísticos por id de producto."""
    for item in items:
        rng = random.Random(item["id"])
        item["precio"] = round(rng.uniform(30.0, 400.0), 2)
        nombre, lat, lng = STORES[item["id"] % len(STORES)]
        item["tienda_nombre"] = nombre
        item["tienda"] = (lat, lng)


# ----------------------------------------------------------------------
# Extracción de características
# ----------------------------------------------------------------------
def extract(items: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """SIFT + K-Means (codebook) + BoVW y embeddings ResNet-18.

    Devuelve ``(codebook, embeddings)``; cada item queda con sus claves
    ``bovw`` (string de tokens) e ``imagen`` (nombre del thumbnail).
    """
    import cv2
    from PIL import Image
    from sklearn.cluster import MiniBatchKMeans

    t0 = time.perf_counter()
    desc_map: dict[int, np.ndarray] = {}
    for item in items:
        img = cv2.imread(item["path"], cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"  [aviso] imagen ilegible: {item['path']}")
            desc_map[item["id"]] = np.empty((0, 128), dtype=np.float32)
            continue
        desc_map[item["id"]] = features.sift_descriptors(img)
    all_desc = np.vstack([d for d in desc_map.values() if len(d)])
    print(f"  SIFT: {all_desc.shape[0]} descriptores de {len(items)} "
          f"imágenes ({time.perf_counter() - t0:.1f}s)")

    t0 = time.perf_counter()
    sample = (all_desc if len(all_desc) <= 50000
              else all_desc[np.random.default_rng(42).choice(
                  len(all_desc), 50000, replace=False)])
    kmeans = MiniBatchKMeans(n_clusters=K_WORDS, batch_size=4096,
                             random_state=42, n_init="auto")
    kmeans.fit(sample)
    codebook = kmeans.cluster_centers_.astype(np.float32)
    print(f"  K-Means: codebook {codebook.shape} entrenado sobre "
          f"{len(sample)} descriptores ({time.perf_counter() - t0:.1f}s)")

    for item in items:
        words = features.visual_words(desc_map[item["id"]], codebook)
        item["bovw"] = " ".join(words)

    t0 = time.perf_counter()
    embeddings = np.empty((len(items), EMB_DIM), dtype=np.float32)
    batch_size = 32
    for lo in range(0, len(items), batch_size):
        chunk = items[lo:lo + batch_size]
        images = []
        valid = []
        for j, item in enumerate(chunk):
            try:
                images.append(Image.open(item["path"]).convert("RGB"))
                valid.append(j)
            except OSError:
                print(f"  [aviso] imagen ilegible: {item['path']}")
        if images:
            feats = features.embed_pil_images(images)
            for j, feat in zip(valid, feats):
                embeddings[lo + j] = feat
        if (lo // batch_size) % 10 == 0:
            print(f"  ResNet-18: {lo + len(chunk)}/{len(items)}",
                  flush=True)
    print(f"  ResNet-18: embeddings {embeddings.shape} "
          f"({time.perf_counter() - t0:.1f}s)")
    return codebook, embeddings


def save_thumbnails(items: list[dict], out_dir: str) -> None:
    """Thumbnails JPEG (lado máx. 256 px) servidos por la API."""
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    for item in items:
        fname = f"{item['id']}.jpg"
        try:
            img = Image.open(item["path"]).convert("RGB")
            img.thumbnail((256, 256))
            img.save(os.path.join(out_dir, fname), "JPEG", quality=85)
        except OSError:
            continue
        item["imagen"] = fname


# ----------------------------------------------------------------------
# Carga en MiniDB
# ----------------------------------------------------------------------
def load_minidb(engine: Engine, items: list[dict],
                embeddings: np.ndarray) -> None:
    """CREATE TABLE + bulk load + los cinco índices del recomendador."""
    def run(sql: str) -> dict:
        res = engine.execute(sql)
        if not res.get("ok"):
            raise SystemExit(f"error ejecutando {sql!r}: "
                             f"{res.get('error')} [{res.get('stage')}]")
        return res

    run(CREATE_TABLE_SQL)
    rows = []
    for line_no, (item, vec) in enumerate(zip(items, embeddings), 1):
        lat, lng = item["tienda"]
        vec_literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        rows.append((line_no, [
            str(item["id"]),
            item["nombre"],
            item["categoria"],
            f"{item['precio']:.2f}",
            item["tienda_nombre"],
            f"({lat}, {lng})",
            item["descripcion"],
            item["bovw"],
            item["imagen"],
            vec_literal,
        ]))
    t0 = time.perf_counter()
    stats = engine.bulk_load_rows(TABLE, rows)
    print(f"  bulk load: {stats['rows_loaded']} filas "
          f"({stats['rows_rejected']} rechazadas) "
          f"en {time.perf_counter() - t0:.1f}s")
    if stats["rows_rejected"]:
        for err in stats["errors"][:10]:
            print(f"    línea {err['line']}: {err['reason']}")

    lists = max(1, int(math.floor(math.sqrt(len(items)))))
    t0 = time.perf_counter()
    run(f"CREATE INDEX ON {TABLE} (precio) USING BTREE;")
    run(f"CREATE INDEX ON {TABLE} (tienda) USING RTREE;")
    run(f"CREATE INDEX ON {TABLE} (descripcion) USING INVERTED "
        f"WITH (language = 'spanish');")
    run(f"CREATE INDEX ON {TABLE} (bovw) USING INVERTED "
        f"WITH (language = 'none');")
    run(f"CREATE INDEX ON {TABLE} (embedding) USING IVF "
        f"WITH (metric = 'COSINE', lists = {lists});")
    print(f"  índices creados ({time.perf_counter() - t0:.1f}s, "
          f"IVF con {lists} listas)")


def reset(engine: Engine) -> None:
    """Borra la tabla, los thumbnails y el codebook anteriores."""
    res = engine.execute(f"DROP TABLE {TABLE};")
    if res.get("ok"):
        print(f"  tabla '{TABLE}' eliminada")
    for path in (features.codebook_path(), features.config_path()):
        if os.path.exists(path):
            os.remove(path)
    img_dir = features.images_dir()
    if os.path.isdir(img_dir):
        for fname in os.listdir(img_dir):
            os.remove(os.path.join(img_dir, fname))
    features.load_codebook.cache_clear()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=("natural", "fashion"),
                    required=True)
    ap.add_argument("--data-dir", required=True,
                    help="directorio del dataset (Natural Images o "
                         "Fashion Product Images descomprimido)")
    ap.add_argument("--limit", type=int, default=None,
                    help="máximo de productos a cargar")
    ap.add_argument("--reset", action="store_true",
                    help="borrar tabla/thumbnails/codebook antes de cargar")
    args = ap.parse_args()

    engine = Engine(DATA_DIR)
    if args.reset:
        print("Reset:")
        reset(engine)

    reader = read_natural if args.source == "natural" else read_fashion
    print(f"Leyendo dataset '{args.source}' desde {args.data_dir} ...")
    items = reader(args.data_dir, args.limit)
    if not items:
        raise SystemExit("no se encontraron imágenes en --data-dir")
    print(f"  {len(items)} productos")
    add_metadata(items)

    print("Extrayendo características (SIFT/K-Means/ResNet-18) ...")
    codebook, embeddings = extract(items)
    np.save(features.codebook_path(), codebook)

    print("Guardando thumbnails ...")
    save_thumbnails(items, features.images_dir())

    print("Cargando en MiniDB ...")
    load_minidb(engine, items, embeddings)

    config = {
        "table": TABLE,
        "source": args.source,
        "products": len(items),
        "embedding_dim": EMB_DIM,
        "bovw_words": K_WORDS,
        "codebook": os.path.basename(features.codebook_path()),
        "images_dir": os.path.basename(features.images_dir()),
        "categories": sorted({i["categoria"] for i in items}),
        "stores": [s[0] for s in STORES],
    }
    with open(features.config_path(), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"Listo: {features.config_path()}")


if __name__ == "__main__":
    main()
