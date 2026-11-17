"""Lógica del recomendador e-commerce sobre MiniDB (Entregable 2).

Traduce la petición del usuario (foto o texto + filtros comerciales) a
sentencias SQL del propio motor, demostrando los cuatro índices a la vez:

- ``precio``  -> B+ Tree (filtros de rango)
- ``tienda``  -> R-Tree (radio alrededor del usuario)
- ``bovw``    -> índice invertido con palabras visuales (paradigma clásico)
- ``embedding`` -> IVF con distancia coseno (paradigma moderno, ANN)

Cada recomendación devuelve las filas, el plan de ejecución y las
métricas de I/O del motor, para mostrarlas en el frontend.
"""

from __future__ import annotations

import json
import os
import random

from ..engine.executor import Engine
from . import features

MAX_K = 100  # tope de seguridad del motor (SELECT_ROW_CAP)

_PRODUCT_COLS = ("id", "nombre", "categoria", "precio", "tienda_nombre",
                 "tienda", "descripcion", "imagen")


def load_config() -> dict | None:
    """``app_config.json`` de la preparación; ``None`` si no existe."""
    try:
        with open(features.config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _escape(text: str) -> str:
    return text.replace("'", "''")


def _where_filters(precio_min: float | None, precio_max: float | None,
                   categoria: str | None, lat: float | None,
                   lng: float | None, radio_km: float | None) -> list[str]:
    """Condiciones WHERE de los filtros comerciales de la petición.

    El radio llega en kilómetros y se convierte a grados (1° ≈ 111 km):
    el R-Tree trabaja con distancia euclídea sobre coordenadas.
    """
    conds: list[str] = []
    if precio_min is not None:
        conds.append(f"precio >= {float(precio_min)}")
    if precio_max is not None:
        conds.append(f"precio <= {float(precio_max)}")
    if categoria:
        conds.append(f"categoria = '{_escape(categoria.strip())}'")
    if lat is not None and lng is not None and radio_km:
        radio_deg = float(radio_km) / 111.0
        conds.append(f"tienda IN (({float(lat)}, {float(lng)}), "
                     f"{radio_deg:.6f})")
    return conds


def _run(engine: Engine, sql: str, probes: int | None = None) -> dict:
    """Ejecuta el SQL (fijando ``ivf.probes`` de sesión si se pide)."""
    if probes is not None:
        res = engine.execute(f"SET ivf.probes = {int(probes)};")
        if not res.get("ok"):
            return res
    return engine.execute(sql)


def _products(result: dict) -> list[dict]:
    """Filas del contrato del motor como dicts columna -> valor."""
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def recommend_by_image(engine: Engine, image_bytes: bytes, *,
                       mode: str = "embedding", k: int = 8,
                       precio_min: float | None = None,
                       precio_max: float | None = None,
                       categoria: str | None = None,
                       lat: float | None = None, lng: float | None = None,
                       radio_km: float | None = None,
                       probes: int | None = None) -> dict:
    """Recomendación por imagen subida.

    ``mode="embedding"``: KNN coseno sobre ``embedding`` (IVF).
    ``mode="bovw"``: Top-K TF-IDF sobre las palabras visuales de la foto
    (paradigma clásico, índice invertido).
    """
    cfg = load_config()
    if cfg is None:
        return {"ok": False, "stage": "execution",
                "error": "el recomendador no está preparado: ejecutar "
                         "scripts/prepare_recommender.py"}
    table = cfg["table"]
    k = max(1, min(int(k), MAX_K))
    conds = _where_filters(precio_min, precio_max, categoria,
                           lat, lng, radio_km)
    cols = ", ".join(_PRODUCT_COLS)
    if mode == "bovw":
        words = features.image_visual_words(image_bytes)
        if not words:
            return {"ok": False, "stage": "execution",
                    "error": "la imagen no produjo palabras visuales "
                             "(SIFT sin keypoints)"}
        where = [f"bovw MATCH '{_escape(' '.join(words))}'", *conds]
        sql = (f"SELECT {cols}, COSINE_SCORE() AS score FROM {table} "
               f"WHERE {' AND '.join(where)} "
               f"ORDER BY score DESC LIMIT {k};")
    else:
        vec = features.image_embedding(image_bytes)
        lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        sql = (f"SELECT {cols}, embedding <=> '{lit}' AS dist "
               f"FROM {table}")
        if conds:
            sql += f" WHERE {' AND '.join(conds)}"
        sql += f" LIMIT {k};"
    result = _run(engine, sql, probes)
    if not result.get("ok"):
        return result
    return {"ok": True, "mode": mode, "sql": sql,
            "products": _products(result),
            "plan": result["plan"], "io": result["io"],
            "elapsed_ms": result["elapsed_ms"],
            "truncated": result.get("truncated", False)}


def recommend_by_text(engine: Engine, text: str, *, k: int = 8,
                      precio_min: float | None = None,
                      precio_max: float | None = None,
                      categoria: str | None = None,
                      lat: float | None = None, lng: float | None = None,
                      radio_km: float | None = None) -> dict:
    """Recomendación por descripción en lenguaje natural (índice
    invertido de ``descripcion`` con ranking TF-IDF coseno)."""
    cfg = load_config()
    if cfg is None:
        return {"ok": False, "stage": "execution",
                "error": "el recomendador no está preparado: ejecutar "
                         "scripts/prepare_recommender.py"}
    if not text or not text.strip():
        return {"ok": False, "stage": "parse",
                "error": "el texto de búsqueda está vacío"}
    table = cfg["table"]
    k = max(1, min(int(k), MAX_K))
    conds = _where_filters(precio_min, precio_max, categoria,
                           lat, lng, radio_km)
    cols = ", ".join(_PRODUCT_COLS)
    where = [f"descripcion MATCH '{_escape(text.strip())}'", *conds]
    sql = (f"SELECT {cols}, COSINE_SCORE() AS score FROM {table} "
           f"WHERE {' AND '.join(where)} "
           f"ORDER BY score DESC LIMIT {k};")
    result = _run(engine, sql)
    if not result.get("ok"):
        return result
    return {"ok": True, "mode": "text", "sql": sql,
            "products": _products(result),
            "plan": result["plan"], "io": result["io"],
            "elapsed_ms": result["elapsed_ms"],
            "truncated": result.get("truncated", False)}


def sample_products(engine: Engine, n: int = 12) -> dict:
    """Muestra aleatoria de productos (para poblar la UI del demo)."""
    cfg = load_config()
    if cfg is None:
        return {"ok": False, "stage": "execution",
                "error": "el recomendador no está preparado: ejecutar "
                         "scripts/prepare_recommender.py"}
    table = cfg["table"]
    cols = ", ".join(_PRODUCT_COLS)
    result = engine.execute(f"SELECT {cols} FROM {table};")
    if not result.get("ok"):
        return result
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    picks = random.sample(rows, min(max(1, n), len(rows)))
    return {"ok": True, "products": picks, "config": cfg}
