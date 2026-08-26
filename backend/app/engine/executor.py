"""Ejecutor: ejecuta el AST y construye el plan con tiempos por paso.

Elige Index Scan cuando existe un índice usable (B+ Tree para igualdad y
rangos, Hash para igualdad, R-Tree para condiciones espaciales) y
Sequential Scan en caso contrario. Cada paso del plan registra su tiempo
en milisegundos, y el detalle del paso indica explícitamente el índice
utilizado, por ejemplo ``USING BTREE ON t.col``.
"""

from __future__ import annotations

import math
import os
import time
from typing import Callable

from ..indexes.btree import BPlusTree
from ..indexes.extendible_hash import ExtendibleHash
from ..indexes.rtree import RTree
from ..storage.heap_file import HeapFile
from ..storage.record import (
    Column,
    SerializationError,
    TYPE_POINT,
    coerce_value,
    decode_key,
    deserialize_row,
    encode_key,
    key_size,
    serialize_row,
)
from .catalog import Catalog
from .csv_loader import (
    CSVError,
    MAX_ERRORS,
    cast_csv_value,
    decode_csv_bytes,
    infer_columns,
    map_columns,
    read_csv,
    reorder_row,
    resolve_dataset_path,
)
from .parser import ParseError, parse


class SemanticError(Exception):
    """Error de validación semántica (tabla/columna/tipo)."""


class ExecutionError(Exception):
    """Error en tiempo de ejecución."""


RID = tuple[int, int]

INDEX_EXT = {"BTREE": "btree", "HASH": "hash", "RTREE": "rtree"}


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


class _Plan:
    """Acumulador de pasos del plan de ejecución con sus tiempos."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, name: str, detail: str, t0: float) -> None:
        self.steps.append(
            {"step": len(self.steps) + 1, "name": name,
             "detail": detail, "time_ms": _ms(t0)}
        )


class Engine:
    """Motor de MiniDB: catálogo + heap files + índices."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.catalog = Catalog(data_dir)

    # ------------------------------------------------------------------
    # Archivos e índices
    # ------------------------------------------------------------------
    def _heap_path(self, table: str) -> str:
        return os.path.join(self.data_dir, f"{table}.heap")

    def _index_path(self, table: str, column: str, itype: str) -> str:
        return os.path.join(
            self.data_dir, f"{table}_{column}.{INDEX_EXT[itype]}"
        )

    def _open_heap(self, table: str, create: bool = False) -> HeapFile:
        return HeapFile(self._heap_path(table), create=create)

    def _open_btree(self, table: str, col: Column,
                    create: bool = False) -> BPlusTree:
        return BPlusTree(
            self._index_path(table, col.name, "BTREE"),
            key_size(col),
            lambda v: encode_key(v, col),
            lambda b: decode_key(b, col),
            create=create,
        )

    def _open_hash(self, table: str, col: Column,
                   create: bool = False) -> ExtendibleHash:
        return ExtendibleHash(
            self._index_path(table, col.name, "HASH"),
            key_size(col),
            lambda v: encode_key(v, col),
            lambda b: decode_key(b, col),
            create=create,
        )

    def _open_rtree(self, table: str, col: Column,
                    create: bool = False) -> RTree:
        return RTree(self._index_path(table, col.name, "RTREE"), create=create)

    # ------------------------------------------------------------------
    # Entrada principal
    # ------------------------------------------------------------------
    def execute(self, sql: str) -> dict:
        """Ejecuta una sentencia SQL y devuelve el resultado del contrato."""
        start = time.perf_counter()
        plan = _Plan()
        try:
            t = time.perf_counter()
            ast = parse(sql)
            plan.add("Parse SQL", f"AST: {ast['type']}", t)
        except ParseError as exc:
            return {"ok": False, "error": str(exc), "stage": "parse"}

        try:
            result = self._dispatch(ast, plan)
        except SemanticError as exc:
            return {"ok": False, "error": str(exc), "stage": "semantic"}
        except SerializationError as exc:
            return {"ok": False, "error": str(exc), "stage": "semantic"}
        except (ExecutionError, KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "stage": "execution"}

        result["ok"] = True
        result["plan"] = plan.steps
        result["elapsed_ms"] = _ms(start)
        return result

    def _dispatch(self, ast: dict, plan: _Plan) -> dict:
        kind = ast["type"]
        if kind == "create_table":
            return self._exec_create_table(ast, plan)
        if kind == "create_table_from_file":
            return self._exec_create_table_from_file(ast, plan)
        if kind == "create_index":
            return self._exec_create_index(ast, plan)
        if kind == "insert":
            return self._exec_insert(ast, plan)
        if kind == "load_file":
            return self._exec_load_file(ast, plan)
        if kind == "select":
            return self._exec_select(ast, plan)
        if kind == "delete":
            return self._exec_delete(ast, plan)
        if kind == "drop_table":
            return self._exec_drop_table(ast, plan)
        raise ExecutionError(f"sentencia no soportada: {kind}")

    # ------------------------------------------------------------------
    # DROP TABLE
    # ------------------------------------------------------------------
    def _exec_drop_table(self, ast: dict, plan: _Plan) -> dict:
        table = ast["table"]
        t = time.perf_counter()
        if not self.catalog.has_table(table):
            raise SemanticError(f"la tabla '{table}' no existe")
        plan.add("Semantic Check", f"'{table}' existe", t)

        t = time.perf_counter()
        paths = [self._heap_path(table)]
        for meta in self.catalog.indexes(table):
            paths.append(self._index_path(table, meta["column"],
                                          meta["type"]))
        removed = 0
        for p in paths:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        plan.add("Drop Files", f"{removed} archivos eliminados de disco", t)

        t = time.perf_counter()
        self.catalog.drop_table(table)
        plan.add("Update Catalog", "catalog.json persistido", t)
        return {"kind": "drop_table",
                "message": f"Tabla '{table}' eliminada"}

    # ------------------------------------------------------------------
    # Utilidades semánticas
    # ------------------------------------------------------------------
    def _get_columns(self, table: str) -> list[Column]:
        if not self.catalog.has_table(table):
            raise SemanticError(f"la tabla '{table}' no existe")
        return self.catalog.columns(table)

    def _get_column(self, table: str, name: str) -> Column:
        col = self.catalog.column(table, name)
        if col is None:
            raise SemanticError(f"la columna '{name}' no existe en '{table}'")
        return col

    # ------------------------------------------------------------------
    # CREATE TABLE
    # ------------------------------------------------------------------
    def _exec_create_table(self, ast: dict, plan: _Plan) -> dict:
        table = ast["table"]
        t = time.perf_counter()
        if self.catalog.has_table(table):
            raise SemanticError(f"la tabla '{table}' ya existe")
        columns = [
            Column(c["name"], c["type"], c["size"], c["primary_key"])
            for c in ast["columns"]
        ]
        names = [c.name for c in columns]
        if len(names) != len(set(names)):
            raise SemanticError("columnas con nombre duplicado")
        plan.add("Semantic Check", f"esquema de '{table}' válido", t)

        t = time.perf_counter()
        heap = self._open_heap(table, create=True)
        heap.close()
        plan.add("Create Heap File", os.path.basename(self._heap_path(table)), t)

        t = time.perf_counter()
        self.catalog.create_table(table, columns)
        plan.add("Update Catalog", "catalog.json persistido", t)
        self._create_pk_index(table, columns, plan)
        return {"kind": "create_table",
                "message": f"Tabla '{table}' creada"}

    def _create_pk_index(self, table: str, columns: list[Column],
                         plan: _Plan) -> None:
        """Crea automáticamente un B+ Tree para la PRIMARY KEY.

        Igual que PostgreSQL, toda PK lleva su índice; además evita que la
        verificación de unicidad en INSERT degrade a un escaneo O(n) por fila.
        """
        pk = next((c for c in columns if c.primary_key), None)
        if pk is None or pk.type == TYPE_POINT:
            return
        t = time.perf_counter()
        idx = self._open_btree(table, pk, create=True)
        idx.close()
        self.catalog.add_index(table, f"{table}_{pk.name}_pk", pk.name, "BTREE")
        plan.add("Create PK Index",
                 f"BTREE automático sobre {table}.{pk.name}", t)

    # ------------------------------------------------------------------
    # CREATE TABLE ... FROM FILE / LOAD INTO ... FROM FILE
    # ------------------------------------------------------------------
    def _read_dataset_csv(self, filename: str) -> tuple[list[str],
                                                        list[tuple[int,
                                                              list[str]]]]:
        """Lee un CSV de ``DATASETS_DIR`` (defecto ``./datasets``)."""
        try:
            path = resolve_dataset_path(
                os.environ.get("DATASETS_DIR", "./datasets"), filename)
        except ValueError as exc:
            raise SemanticError(str(exc)) from exc
        if not os.path.isfile(path):
            raise ExecutionError(
                f"el archivo '{filename}' no existe en DATASETS_DIR")
        with open(path, "rb") as f:
            text = decode_csv_bytes(f.read())
        try:
            return read_csv(text)
        except CSVError as exc:
            raise ExecutionError(str(exc)) from exc

    def _exec_create_table_from_file(self, ast: dict, plan: _Plan) -> dict:
        table, filename = ast["table"], ast["file"]
        t = time.perf_counter()
        if self.catalog.has_table(table):
            raise SemanticError(f"la tabla '{table}' ya existe")
        plan.add("Semantic Check", f"'{table}' no existe aún", t)

        t = time.perf_counter()
        header, rows = self._read_dataset_csv(filename)
        columns = infer_columns(header, [r for _, r in rows])
        pk = next((c.name for c in columns if c.primary_key), None)
        plan.add("Infer Schema",
                 f"{len(columns)} columnas inferidas de '{filename}'"
                 + (f", PK sugerida: {pk}" if pk else ""), t)

        t = time.perf_counter()
        heap = self._open_heap(table, create=True)
        heap.close()
        plan.add("Create Heap File", os.path.basename(self._heap_path(table)), t)

        t = time.perf_counter()
        self.catalog.create_table(table, columns)
        plan.add("Update Catalog", "catalog.json persistido", t)
        self._create_pk_index(table, columns, plan)

        t = time.perf_counter()
        stats = self.bulk_load_rows(table, rows)
        plan.add("Bulk Load",
                 f"{stats['rows_loaded']} filas cargadas, "
                 f"{stats['rows_rejected']} rechazadas desde '{filename}'", t)
        return {"kind": "create_table",
                "message": f"Tabla '{table}' creada desde {filename}: "
                           f"{stats['rows_loaded']} filas cargadas, "
                           f"{stats['rows_rejected']} rechazadas",
                "load_errors": stats["errors"]}

    def _exec_load_file(self, ast: dict, plan: _Plan) -> dict:
        table, filename = ast["table"], ast["file"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        plan.add("Semantic Check", f"tabla '{table}' válida", t)

        t = time.perf_counter()
        header, rows = self._read_dataset_csv(filename)
        try:
            positions, ignored = map_columns(header, columns)
        except ValueError as exc:
            raise SemanticError(str(exc)) from exc
        plan.add("Map Columns",
                 f"{len(positions)} columnas mapeadas por nombre"
                 + (f", ignoradas: {', '.join(ignored)}" if ignored else ""),
                 t)

        t = time.perf_counter()
        mapped = [(ln, reorder_row(raw, positions)) for ln, raw in rows]
        stats = self.bulk_load_rows(table, mapped)
        plan.add("Bulk Load",
                 f"{stats['rows_loaded']} filas cargadas, "
                 f"{stats['rows_rejected']} rechazadas desde '{filename}'", t)
        return {"kind": "insert",
                "rowcount": stats["rows_loaded"],
                "message": f"{stats['rows_loaded']} filas cargadas en "
                           f"'{table}' desde {filename} "
                           f"({stats['rows_rejected']} rechazadas)",
                "load_errors": stats["errors"],
                "ignored_columns": ignored}

    # ------------------------------------------------------------------
    # CREATE INDEX
    # ------------------------------------------------------------------
    def _exec_create_index(self, ast: dict, plan: _Plan) -> dict:
        table, col_name, itype = ast["table"], ast["column"], ast["index_type"]
        t = time.perf_counter()
        self._get_columns(table)
        col = self._get_column(table, col_name)
        if itype == "RTREE":
            if col.type != TYPE_POINT:
                raise SemanticError("RTREE solo se permite sobre columnas POINT")
        elif col.type == TYPE_POINT:
            raise SemanticError(f"{itype} no soporta columnas POINT")
        if self.catalog.index_on(table, col_name, {itype}):
            raise SemanticError(
                f"ya existe un índice {itype} sobre {table}.{col_name}")
        idx_name = ast["name"] or f"{table}_{col_name}_{itype.lower()}"
        plan.add("Semantic Check",
                 f"{itype} sobre {table}.{col_name} es válido", t)

        t = time.perf_counter()
        heap = self._open_heap(table)
        if itype == "BTREE":
            idx = self._open_btree(table, col, create=True)
        elif itype == "HASH":
            idx = self._open_hash(table, col, create=True)
        else:
            idx = self._open_rtree(table, col, create=True)
        plan.add("Create Index File",
                 os.path.basename(self._index_path(table, col_name, itype)), t)

        t = time.perf_counter()
        col_pos = [c.name for c in self.catalog.columns(table)].index(col_name)
        n = 0
        for rid, raw in heap.scan():
            row = deserialize_row(self.catalog.columns(table), raw)
            value = row[col_pos]
            if itype == "RTREE":
                idx.insert(tuple(value), rid)
            else:
                idx.insert(value, rid)
            n += 1
        idx.close()
        heap.close()
        plan.add("Bulk Load", f"{n} registros indexados con {itype}", t)

        t = time.perf_counter()
        self.catalog.add_index(table, idx_name, col_name, itype)
        plan.add("Update Catalog", "catalog.json persistido", t)
        return {"kind": "create_index",
                "message": f"Índice '{idx_name}' ({itype}) creado "
                           f"sobre {table}.{col_name}"}

    # ------------------------------------------------------------------
    # INSERT
    # ------------------------------------------------------------------
    def _exec_insert(self, ast: dict, plan: _Plan) -> dict:
        table = ast["table"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        if len(ast["values"]) != len(columns):
            raise SemanticError(
                f"'{table}' tiene {len(columns)} columnas, "
                f"se recibieron {len(ast['values'])} valores")
        row = [coerce_value(v, c) for v, c in zip(ast["values"], columns)]
        plan.add("Semantic Check", f"tipos válidos para '{table}'", t)

        self._insert_row(table, columns, row, plan)
        return {"kind": "insert", "rowcount": 1, "message": "1 registro insertado"}

    def _insert_row(self, table: str, columns: list[Column], row: list,
                    plan: _Plan | None = None) -> RID:
        """Inserta una fila validada manteniendo heap file e índices.

        Verifica la unicidad de la PRIMARY KEY (índice si existe, si no
        escaneo secuencial) y devuelve el RID asignado.
        """
        pk = self.catalog.primary_key(table)
        if pk is not None:
            t = time.perf_counter()
            pk_pos = [c.name for c in columns].index(pk.name)
            value = row[pk_pos]
            idx_meta = self.catalog.index_on(table, pk.name, {"BTREE", "HASH"})
            if idx_meta is not None:
                idx = (self._open_btree(table, pk) if idx_meta["type"] == "BTREE"
                       else self._open_hash(table, pk))
                found = idx.search(value)
                idx.close()
                if plan is not None:
                    plan.add("Index Lookup",
                             f"USING {idx_meta['type']} ON {table}.{pk.name}", t)
            else:
                heap = self._open_heap(table)
                found = []
                for rid, raw in heap.scan():
                    if deserialize_row(columns, raw)[pk_pos] == value:
                        found.append(rid)
                heap.close()
                if plan is not None:
                    plan.add("Sequential Scan",
                             f"verificación de PK en {table}.{pk.name}", t)
            if found:
                raise ExecutionError(
                    f"clave primaria duplicada: {pk.name} = {value!r}")

        t = time.perf_counter()
        heap = self._open_heap(table)
        rid = heap.insert(serialize_row(columns, row))
        heap.close()
        if plan is not None:
            plan.add("Heap Insert", f"RID = ({rid[0]}, {rid[1]})", t)

        for meta in self.catalog.indexes(table):
            t = time.perf_counter()
            col = self._get_column(table, meta["column"])
            col_pos = [c.name for c in columns].index(col.name)
            value = row[col_pos]
            if meta["type"] == "BTREE":
                idx = self._open_btree(table, col)
            elif meta["type"] == "HASH":
                idx = self._open_hash(table, col)
            else:
                idx = self._open_rtree(table, col)
            idx.insert(tuple(value) if col.type == TYPE_POINT else value, rid)
            idx.close()
            if plan is not None:
                plan.add("Index Maintenance",
                         f"{meta['type']} ON {table}.{col.name} actualizado", t)
        return rid

    def bulk_load_rows(self, table: str,
                       rows: list[tuple[int, list[str]]]) -> dict:
        """Carga masiva de filas crudas (strings) en una tabla existente.

        Cada elemento es ``(número de línea, valores crudos)`` ya
        ordenados según las columnas de la tabla. Las filas inválidas se
        rechazan sin abortar la carga; se conservan hasta ``MAX_ERRORS``
        errores con su número de línea.
        """
        columns = self._get_columns(table)
        loaded = rejected = 0
        errors: list[dict] = []
        for line_no, raw_row in rows:
            if len(raw_row) != len(columns):
                rejected += 1
                if len(errors) < MAX_ERRORS:
                    errors.append({
                        "line": line_no,
                        "reason": f"se esperaban {len(columns)} valores, "
                                  f"llegaron {len(raw_row)}",
                    })
                continue
            try:
                row = [cast_csv_value(v, c)
                       for v, c in zip(raw_row, columns)]
                self._insert_row(table, columns, row)
            except (ValueError, SerializationError, ExecutionError) as exc:
                rejected += 1
                if len(errors) < MAX_ERRORS:
                    errors.append({"line": line_no, "reason": str(exc)})
                continue
            loaded += 1
        return {"rows_loaded": loaded, "rows_rejected": rejected,
                "errors": errors}

    # ------------------------------------------------------------------
    # SELECT
    # ------------------------------------------------------------------
    def _exec_select(self, ast: dict, plan: _Plan) -> dict:
        table = ast["table"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        col_map = {c.name: c for c in columns}
        if ast["columns"] == ["*"]:
            selected = [c.name for c in columns]
        else:
            for name in ast["columns"]:
                if name not in col_map:
                    raise SemanticError(
                        f"la columna '{name}' no existe en '{table}'")
            selected = ast["columns"]
        where = ast["where"]
        if where is not None:
            self._get_column(table, where["column"])
        plan.add("Semantic Check", f"tabla '{table}' y columnas válidas", t)

        # Acceso a datos: índice si existe, si no escaneo secuencial
        rids: list[RID] | None = None  # None => escaneo secuencial
        ordered_knn: list[tuple[RID, float]] | None = None
        if where is not None:
            rids, ordered_knn = self._plan_access(table, where, plan)

        t = time.perf_counter()
        heap = self._open_heap(table)
        if rids is None:  # escaneo secuencial
            rows = [(rid, deserialize_row(columns, raw))
                    for rid, raw in heap.scan()]
            if where is not None:
                rows = [(rid, row) for rid, row in rows
                        if self._match(columns, where, row)]
            if where is not None and where["kind"] == "knn":
                # KNN por fuerza bruta cuando no hay R-Tree
                pos = [c.name for c in columns].index(where["column"])
                cx, cy = where["center"]
                rows.sort(key=lambda t: math.hypot(t[1][pos][0] - cx,
                                                   t[1][pos][1] - cy))
                rows = rows[: where["k"]]
        elif ordered_knn is not None:
            by_rid = dict()
            for rid, d in ordered_knn:
                by_rid[rid] = deserialize_row(columns, heap.read(rid))
            rows = [(rid, by_rid[rid]) for rid, _ in ordered_knn]
        else:
            rows = [(rid, deserialize_row(columns, heap.read(rid)))
                    for rid in rids]
        heap.close()
        plan.add("Fetch Rows", f"{len(rows)} registros leídos del heap", t)

        t = time.perf_counter()
        positions = [[c.name for c in columns].index(name) for name in selected]
        out_rows = [[row[i] for i in positions] for _, row in rows]
        if ast["limit"] is not None and ordered_knn is None:
            out_rows = out_rows[: ast["limit"]]
        plan.add("Projection", f"columnas: {', '.join(selected)}", t)
        if ast["limit"] is not None:
            t = time.perf_counter()
            plan.add("Limit", f"máximo {ast['limit']} filas", t)

        spatial = self._spatial_payload(columns, selected, out_rows)
        return {
            "kind": "select",
            "columns": selected,
            "rows": [[list(v) if isinstance(v, tuple) else v for v in row]
                     for row in out_rows],
            "rowcount": len(out_rows),
            "message": "OK",
            "spatial": spatial,
        }

    def _plan_access(self, table: str, where: dict,
                     plan: _Plan) -> tuple[list[RID] | None,
                                           list[tuple[RID, float]] | None]:
        """Elige el método de acceso y devuelve RIDs (None = seq scan)."""
        col = self._get_column(table, where["column"])
        kind = where["kind"]

        if kind == "compare":
            op, value = where["op"], where["value"]
            value = coerce_value(value, col)
            if op == "=" and col.type != TYPE_POINT:
                meta = self.catalog.index_on(table, col.name, {"BTREE", "HASH"})
                if meta is not None:
                    t = time.perf_counter()
                    idx = (self._open_btree(table, col)
                           if meta["type"] == "BTREE"
                           else self._open_hash(table, col))
                    rids = idx.search(value)
                    idx.close()
                    plan.add("Index Scan",
                             f"USING {meta['type']} ON {table}.{col.name} "
                             f"= {value!r} -> {len(rids)} RIDs", t)
                    return rids, None
            elif op in ("<", "<=", ">", ">=") and col.type != TYPE_POINT:
                meta = self.catalog.index_on(table, col.name, {"BTREE"})
                if meta is not None:
                    t = time.perf_counter()
                    idx = self._open_btree(table, col)
                    if op == "<":
                        rids = idx.range_search(hi=value, hi_inc=False)
                    elif op == "<=":
                        rids = idx.range_search(hi=value)
                    elif op == ">":
                        rids = idx.range_search(lo=value, lo_inc=False)
                    else:
                        rids = idx.range_search(lo=value)
                    idx.close()
                    plan.add("Index Range Scan",
                             f"USING BTREE ON {table}.{col.name} {op} {value!r} "
                             f"-> {len(rids)} RIDs", t)
                    return rids, None
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin índice usable para {table}.{col.name} {op}", t)
            return None, None

        if kind == "between":
            low = coerce_value(where["low"], col)
            high = coerce_value(where["high"], col)
            meta = (None if col.type == TYPE_POINT
                    else self.catalog.index_on(table, col.name, {"BTREE"}))
            if meta is not None:
                t = time.perf_counter()
                idx = self._open_btree(table, col)
                rids = idx.range_search(low, high)
                idx.close()
                plan.add("Index Range Scan",
                         f"USING BTREE ON {table}.{col.name} "
                         f"BETWEEN {low!r} AND {high!r} -> {len(rids)} RIDs", t)
                return rids, None
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin índice BTREE para {table}.{col.name}", t)
            return None, None

        if kind in ("radius", "knn"):
            if col.type != TYPE_POINT:
                raise SemanticError(
                    f"la condición espacial requiere una columna POINT, "
                    f"'{col.name}' es {col.type_str()}")
            meta = self.catalog.index_on(table, col.name, {"RTREE"})
            if meta is not None:
                t = time.perf_counter()
                idx = self._open_rtree(table, col)
                if kind == "radius":
                    rids = idx.search_radius(where["center"], where["radius"])
                    idx.close()
                    plan.add("R-Tree Radius Search",
                             f"USING RTREE ON {table}.{col.name} "
                             f"centro={where['center']} r={where['radius']} "
                             f"-> {len(rids)} RIDs", t)
                    return rids, None
                results = idx.knn(where["center"], where["k"])
                idx.close()
                plan.add("R-Tree KNN Search",
                         f"USING RTREE ON {table}.{col.name} "
                         f"centro={where['center']} k={where['k']}", t)
                return [rid for rid, _ in results], results
            # Respaldo sin índice: fuerza bruta
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin RTREE en {table}.{col.name}: "
                     f"cómputo espacial por fuerza bruta", t)
            return None, None

        raise ExecutionError(f"condición no soportada: {kind}")

    # ------------------------------------------------------------------
    # Filtro en escaneo secuencial
    # ------------------------------------------------------------------
    def _match(self, columns: list[Column], where: dict, row: list) -> bool:
        col = next(c for c in columns if c.name == where["column"])
        pos = [c.name for c in columns].index(col.name)
        value = row[pos]
        kind = where["kind"]
        if kind == "compare":
            try:
                target = coerce_value(where["value"], col)
            except SerializationError:
                return False
            op = where["op"]
            if col.type == TYPE_POINT:
                return op == "=" and tuple(value) == tuple(target)
            return {"=": value == target, "<": value < target,
                    "<=": value <= target, ">": value > target,
                    ">=": value >= target}[op]
        if kind == "between":
            try:
                low = coerce_value(where["low"], col)
                high = coerce_value(where["high"], col)
            except SerializationError:
                return False
            return low <= value <= high
        if kind == "radius":
            cx, cy = where["center"]
            return math.hypot(value[0] - cx, value[1] - cy) <= where["radius"]
        if kind == "knn":
            return True  # el filtro KNN sin índice se aplica tras ordenar
        return False

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------
    def _exec_delete(self, ast: dict, plan: _Plan) -> dict:
        table = ast["table"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        col = self._get_column(table, ast["column"])
        value = coerce_value(ast["value"], col)
        plan.add("Semantic Check", f"condición sobre {table}.{col.name}", t)

        where = {"kind": "compare", "column": col.name, "op": "=",
                 "value": ast["value"]}
        rids, _ = self._plan_access(table, where, plan)
        if rids is None:
            t = time.perf_counter()
            heap = self._open_heap(table)
            rids = [rid for rid, raw in heap.scan()
                    if self._match(columns, where, deserialize_row(columns, raw))]
            heap.close()
            plan.add("Filter", f"{len(rids)} registros coinciden", t)

        t = time.perf_counter()
        heap = self._open_heap(table)
        victims = [(rid, deserialize_row(columns, heap.read(rid)))
                   for rid in rids]
        for rid, _ in victims:
            heap.delete(rid)
        heap.close()
        plan.add("Heap Delete", f"{len(victims)} registros eliminados", t)

        for meta in self.catalog.indexes(table):
            t = time.perf_counter()
            icol = self._get_column(table, meta["column"])
            pos = [c.name for c in columns].index(icol.name)
            if meta["type"] == "BTREE":
                idx = self._open_btree(table, icol)
            elif meta["type"] == "HASH":
                idx = self._open_hash(table, icol)
            else:
                idx = self._open_rtree(table, icol)
            for rid, row in victims:
                v = row[pos]
                idx.delete(tuple(v) if icol.type == TYPE_POINT else v, rid)
            idx.close()
            plan.add("Index Maintenance",
                     f"{meta['type']} ON {table}.{icol.name} actualizado", t)

        return {"kind": "delete", "rowcount": len(victims),
                "message": f"{len(victims)} registro(s) eliminado(s)"}

    # ------------------------------------------------------------------
    # Payload espacial e info de tablas
    # ------------------------------------------------------------------
    def _spatial_payload(self, columns: list[Column], selected: list[str],
                         rows: list[list]) -> dict | None:
        """``spatial`` del contrato: puntos de la primera columna POINT."""
        col_positions = {c.name: i for i, c in enumerate(columns)}
        point_cols = [c for c in columns
                      if c.type == TYPE_POINT and c.name in selected]
        if not point_cols:
            return None
        col = point_cols[0]
        pos = selected.index(col.name)
        points = []
        for row in rows:
            x, y = row[pos]
            points.append({
                "x": x, "y": y,
                "row": [list(v) if isinstance(v, tuple) else v for v in row],
            })
        return {"column": col.name, "points": points}

    def table_info(self) -> list[dict]:
        """Información de tablas para ``GET /api/tables``."""
        out = []
        for name in self.catalog.table_names():
            columns = self.catalog.columns(name)
            heap = self._open_heap(name)
            rowcount = heap.row_count
            heap.close()
            files = [self._file_info(self._heap_path(name))]
            for meta in self.catalog.indexes(name):
                files.append(self._file_info(
                    self._index_path(name, meta["column"], meta["type"])))
            out.append({
                "name": name,
                "columns": [
                    {"name": c.name, "type": c.type_str(),
                     "primary_key": c.primary_key}
                    for c in columns
                ],
                "indexes": [
                    {"name": m["name"], "column": m["column"],
                     "type": m["type"]}
                    for m in self.catalog.indexes(name)
                ],
                "rowcount": rowcount,
                "files": files,
            })
        return out

    @staticmethod
    def _file_info(path: str) -> dict:
        size = os.path.getsize(path)
        pages = (size + 4095) // 4096
        return {"path": os.path.abspath(path), "size_bytes": size,
                "pages": pages}
