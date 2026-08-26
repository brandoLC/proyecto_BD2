"""Endpoints REST de MiniDB (contrato fijado con el frontend).

- ``GET  /api/health`` -> ``{"status": "ok"}``
- ``GET  /api/tables`` -> tablas con columnas, índices, rowcount y archivos
- ``POST /api/query``  -> ejecuta una sentencia SQL del subconjunto MiniDB
- ``POST /api/infer-schema`` -> infiere el esquema de un CSV subido
- ``POST /api/tables/{name}/upload-csv`` -> carga un CSV en una tabla
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from ..engine.csv_loader import (
    CSVError,
    decode_csv_bytes,
    infer_columns,
    map_columns,
    read_csv,
    reorder_row,
    sanitize_identifier,
    suggested_create_sql,
)
from ..engine.executor import Engine

DATA_DIR = os.environ.get("DATA_DIR", "./data")

router = APIRouter()
engine = Engine(DATA_DIR)


class QueryRequest(BaseModel):
    sql: str


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/tables")
def tables() -> dict:
    return {"tables": engine.table_info()}


@router.post("/query")
def query(req: QueryRequest) -> dict:
    if not req.sql or not req.sql.strip():
        return {"ok": False, "error": "la sentencia SQL está vacía",
                "stage": "parse"}
    return engine.execute(req.sql)


@router.post("/infer-schema")
async def infer_schema(file: UploadFile = File(...),
                       table_name: str | None = Form(default=None)) -> dict:
    """Infiera el esquema de un CSV subido (multipart, campo ``file``)."""
    try:
        text = decode_csv_bytes(await file.read())
        header, rows = read_csv(text)
    except CSVError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}
    if table_name and table_name.strip():
        name = sanitize_identifier(table_name)
    else:
        base = os.path.splitext(os.path.basename(file.filename or ""))[0]
        name = sanitize_identifier(base or "tabla")
    columns = infer_columns(header, [r for _, r in rows])
    return {
        "ok": True,
        "table_name": name,
        "columns": [
            {"name": c.name, "type": c.type_str(), "primary_key": c.primary_key}
            for c in columns
        ],
        "suggested_sql": suggested_create_sql(name, columns),
        "preview_rows": [row for _, row in rows[:5]],
        "total_rows_estimate": len(rows),
    }


@router.post("/tables/{name}/upload-csv")
async def upload_csv(name: str, file: UploadFile = File(...)) -> dict:
    """Carga un CSV en una tabla existente mapeando por nombre de columna.

    Las filas inválidas se rechazan (sin abortar la carga) y se reportan
    con su número de línea; las columnas extra del CSV se ignoran.
    """
    start = time.perf_counter()
    if not engine.catalog.has_table(name):
        return {"ok": False, "error": f"la tabla '{name}' no existe",
                "stage": "semantic"}
    try:
        text = decode_csv_bytes(await file.read())
        header, rows = read_csv(text)
    except CSVError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}
    columns = engine.catalog.columns(name)
    try:
        positions, ignored = map_columns(header, columns)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "stage": "semantic"}
    mapped = [(ln, reorder_row(raw, positions)) for ln, raw in rows]
    stats = engine.bulk_load_rows(name, mapped)
    return {
        "ok": True,
        "rows_loaded": stats["rows_loaded"],
        "rows_rejected": stats["rows_rejected"],
        "errors": stats["errors"],
        "ignored_columns": ignored,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
    }
