"""Catálogo del sistema, persistido como JSON en el directorio de datos.

Guarda, por tabla: columnas (nombre, tipo, tamaño, clave primaria) e
índices (nombre, columna, tipo). Se carga al iniciar el motor y cada
sentencia DDL lo actualiza y lo persiste en disco.
"""

from __future__ import annotations

import json
import os

from ..storage.record import Column

INDEX_TYPES = {"BTREE", "HASH", "RTREE"}


class Catalog:
    """Catálogo de tablas e índices, persistido en ``catalog.json``."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "catalog.json")
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {"tables": {}}
            self._save()

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Tablas
    # ------------------------------------------------------------------
    def table_names(self) -> list[str]:
        return sorted(self._data["tables"].keys())

    def has_table(self, name: str) -> bool:
        return name in self._data["tables"]

    def create_table(self, name: str, columns: list[Column]) -> None:
        self._data["tables"][name] = {
            "columns": [c.to_dict() for c in columns],
            "indexes": [],
        }
        self._save()

    def columns(self, table: str) -> list[Column]:
        return [Column.from_dict(d) for d in self._data["tables"][table]["columns"]]

    def column(self, table: str, col_name: str) -> Column | None:
        for c in self.columns(table):
            if c.name == col_name:
                return c
        return None

    def primary_key(self, table: str) -> Column | None:
        for c in self.columns(table):
            if c.primary_key:
                return c
        return None

    # ------------------------------------------------------------------
    # Índices
    # ------------------------------------------------------------------
    def add_index(self, table: str, name: str, column: str, itype: str) -> None:
        self._data["tables"][table]["indexes"].append(
            {"name": name, "column": column, "type": itype}
        )
        self._save()

    def indexes(self, table: str) -> list[dict]:
        return list(self._data["tables"][table]["indexes"])

    def index_on(self, table: str, column: str,
                 itypes: set[str] | None = None) -> dict | None:
        """Primer índice sobre la columna, opcionalmente filtrado por tipo."""
        for idx in self.indexes(table):
            if idx["column"] == column and (itypes is None or idx["type"] in itypes):
                return idx
        return None
