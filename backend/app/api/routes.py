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
    column_position,
    decode_csv_bytes,
    detect_point_pair,
    infer_columns,
    map_columns,
    read_csv,
    reorder_row,
    sanitize_identifier,
    suggested_create_sql,
)
from ..engine.executor import Engine
from ..storage.record import Column, TYPE_POINT

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
    det = detect_point_pair(header, columns, [r for _, r in rows])
    sql_columns = columns
    notes: list[str] = []
    if det is not None:
        sql_columns = columns + [Column(det["column"], TYPE_POINT)]
        notes.append(
            f"columna '{det['column']}' (POINT) derivada de "
            f"'{det['lat_col']}' + '{det['lng_col']}'")
    return {
        "ok": True,
        "table_name": name,
        "columns": [
            {"name": c.name, "type": c.type_str(), "primary_key": c.primary_key}
            for c in columns
        ],
        "suggested_sql": suggested_create_sql(name, sql_columns),
        "preview_rows": [row for _, row in rows[:5]],
        "total_rows_estimate": len(rows),
        "derived_point": det,
        "notes": notes,
    }


@router.post("/tables/{name}/upload-csv")
async def upload_csv(name: str, file: UploadFile = File(...),
                     point_column: str | None = Form(default=None),
                     lat_col: str | None = Form(default=None),
                     lng_col: str | None = Form(default=None)) -> dict:
    """Carga un CSV en una tabla existente mapeando por nombre de columna.

    Las filas inválidas se rechazan (sin abortar la carga) y se reportan
    con su número de línea; las columnas extra del CSV se ignoran. Con
    los campos opcionales ``point_column``/``lat_col``/``lng_col`` (van
    juntos o no van) se deriva el valor de la columna POINT
    ``point_column`` a partir de las columnas ``lat_col`` y ``lng_col``
    del CSV.
    """
    start = time.perf_counter()
    if not engine.catalog.has_table(name):
        return {"ok": False, "error": f"la tabla '{name}' no existe",
                "stage": "semantic"}
    given = [v for v in (point_column, lat_col, lng_col) if v and v.strip()]
    if given and len(given) < 3:
        return {"ok": False,
                "error": "point_column, lat_col y lng_col deben enviarse "
                         "juntos (o ninguno)",
                "stage": "semantic"}
    try:
        text = decode_csv_bytes(await file.read())
        header, rows = read_csv(text)
    except CSVError as exc:
        return {"ok": False, "error": str(exc), "stage": "parse"}
    columns = engine.catalog.columns(name)

    point_col: Column | None = None
    derive: tuple[int, str] | None = None
    lat_i = lng_i = None
    if given:
        point_col = next(
            (c for c in columns
             if c.name.lower() == point_column.strip().lower()), None)
        if point_col is None:
            return {"ok": False,
                    "error": f"la tabla '{name}' no tiene la columna "
                             f"'{point_column}'",
                    "stage": "semantic"}
        if point_col.type != TYPE_POINT:
            return {"ok": False,
                    "error": f"la columna '{point_col.name}' no es POINT",
                    "stage": "semantic"}
        lat_i = column_position(header, lat_col)
        lng_i = column_position(header, lng_col)
        if lat_i is None or lng_i is None:
            falta = lat_col if lat_i is None else lng_col
            return {"ok": False,
                    "error": f"el CSV no tiene la columna '{falta}'",
                    "stage": "semantic"}

    try:
        positions, ignored = map_columns(
            header,
            [c for c in columns if c is not point_col]
            if point_col is not None else columns)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "stage": "semantic"}
    if point_col is not None:
        mapped = [
            (ln, reorder_row(raw, positions)
             + [raw[lat_i] if lat_i < len(raw) else "",
                raw[lng_i] if lng_i < len(raw) else ""])
            for ln, raw in rows
        ]
        derive = ([c.name for c in columns].index(point_col.name),
                  point_col.name)
    else:
        mapped = [(ln, reorder_row(raw, positions)) for ln, raw in rows]
    stats = engine.bulk_load_rows(name, mapped, derive=derive)
    return {
        "ok": True,
        "rows_loaded": stats["rows_loaded"],
        "rows_rejected": stats["rows_rejected"],
        "errors": stats["errors"],
        "ignored_columns": ignored,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
    }
