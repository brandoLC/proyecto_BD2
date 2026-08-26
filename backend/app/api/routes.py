"""Endpoints REST de MiniDB (contrato fijado con el frontend).

- ``GET  /api/health`` -> ``{"status": "ok"}``
- ``GET  /api/tables`` -> tablas con columnas, índices, rowcount y archivos
- ``POST /api/query``  -> ejecuta una sentencia SQL del subconjunto MiniDB
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel

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
