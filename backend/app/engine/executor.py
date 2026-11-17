"""Ejecutor: ejecuta el AST y construye el plan con tiempos por paso.

Elige Index Scan cuando existe un índice usable (B+ Tree para igualdad y
rangos, Hash para igualdad, R-Tree para condiciones espaciales) y
Sequential Scan en caso contrario. Cada paso del plan registra su tiempo
en milisegundos, y el detalle del paso indica explícitamente el índice
utilizado, por ejemplo ``USING BTREE ON t.col``.
"""

from __future__ import annotations

import collections
import math
import os
import time
from typing import Callable

from ..indexes.btree import BPlusTree
from ..indexes.extendible_hash import ExtendibleHash
from ..indexes.inverted_index import InvertedIndex
from ..indexes.ivf import IVFIndex, METRICS as IVF_METRICS, vector_distance
from ..indexes.rtree import RTree
from ..storage.disk_counter import DiskCounter
from ..storage.heap_file import HeapFile
from ..storage.sequential_file import SequentialFile
from ..storage.record import (
    Column,
    SerializationError,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_POINT,
    TYPE_TEXT,
    TYPE_VARCHAR,
    TYPE_VECTOR,
    coerce_value,
    coerce_vector,
    decode_key,
    deserialize_row,
    encode_key,
    key_size,
    serialize_row,
)
from ..text.preprocess import TextPreprocessor
from ..text.query_parser import MatchQueryError, parse_match_query
from .catalog import Catalog
from .csv_loader import (
    CSVError,
    MAX_ERRORS,
    cast_csv_value,
    column_position,
    decode_csv_bytes,
    derive_point_value,
    detect_point_pair,
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

# Archivos físicos que componen un índice INVERTED (índice invertido):
# diccionario, listas de posteo, mapa de documentos y normas L2.
INVERTED_EXTS = ("dic", "inv", "docs", "nrm")

# Archivos físicos de un índice IVF: directorio de centroides y listas
# invertidas paginadas.
IVF_EXTS = ("cen", "lst")

# A partir de este número de filas, la verificación de unicidad de la PK
# en carga masiva siembra un conjunto con las claves existentes en vez de
# consultar el índice fila por fila (equivalente y mucho más rápido).
_BULK_PK_SET_MIN = 512

# Tope de seguridad anti-congelamiento del cliente: un SELECT sin LIMIT
# explícito devuelve como máximo estas filas y marca la respuesta con
# ``"truncated": true`` para que el cliente pagine. Un LIMIT explícito del
# usuario (aunque sea mayor que este tope) siempre se respeta completo y no
# marca truncated. El escaneo se detiene apenas supera este conteo (sondea
# una fila extra para saber si el total lo excede) en lugar de materializar
# y serializar millones de filas.
SELECT_ROW_CAP = 100


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
        self._auto_cache: dict[str, int] = {}  # siguiente valor por tabla
        self._seq_dirty: set[str] = set()
        # Parámetros de sesión (sentencia SET): p. ej. {"ivf.probes": 5}
        self._session: dict[str, int] = {}
        # Contador de I/O de la consulta en curso (None fuera de execute).
        self._counter: DiskCounter | None = None

    # ------------------------------------------------------------------
    # Archivos e índices
    # ------------------------------------------------------------------
    def _heap_path(self, table: str) -> str:
        return os.path.join(self.data_dir, f"{table}.heap")

    def _index_path(self, table: str, column: str, itype: str) -> str:
        return os.path.join(
            self.data_dir, f"{table}_{column}.{INDEX_EXT[itype]}"
        )

    def _index_paths(self, table: str, column: str, itype: str) -> list[str]:
        """Todos los archivos físicos del índice (varios para INVERTED/IVF)."""
        if itype == "INVERTED":
            base = os.path.join(self.data_dir, f"{table}_{column}")
            return [f"{base}.{ext}" for ext in INVERTED_EXTS]
        if itype == "IVF":
            base = os.path.join(self.data_dir, f"{table}_{column}")
            return [f"{base}.{ext}" for ext in IVF_EXTS]
        return [self._index_path(table, column, itype)]

    def _open_heap(self, table: str, create: bool = False) -> HeapFile:
        return HeapFile(self._heap_path(table), create=create,
                        counter=self._counter)

    def _seq_paths(self, table: str) -> tuple[str, str]:
        return (os.path.join(self.data_dir, f"{table}.seq"),
                os.path.join(self.data_dir, f"{table}.ovf"))

    def _is_sequential(self, table: str) -> bool:
        return self.catalog.organization(table) == "sequential"

    def _open_sequential(self, table: str, pk: Column,
                         create: bool = False) -> SequentialFile:
        seq_path, ovf_path = self._seq_paths(table)
        return SequentialFile(
            seq_path, ovf_path,
            key_size(pk),
            lambda v: encode_key(v, pk),
            lambda b: decode_key(b, pk),
            create=create,
            counter=self._counter,
        )

    def _open_storage(self, table: str, create: bool = False):
        """Archivo de datos de la tabla: HeapFile o SequentialFile según
        su organización en el catálogo."""
        if self._is_sequential(table):
            return self._open_sequential(
                table, self.catalog.primary_key(table), create=create)
        return self._open_heap(table, create=create)

    @staticmethod
    def _scan_rows(storage):
        """Itera ``(rid, record_bytes)`` uniforme sobre HeapFile y
        SequentialFile (cuyo scan también devuelve la clave)."""
        if isinstance(storage, SequentialFile):
            for rid, _key, raw in storage.scan():
                yield rid, raw
        else:
            yield from storage.scan()

    @staticmethod
    def _read_row(storage, rid) -> bytes:
        """Registro crudo del RID, uniforme para ambas organizaciones."""
        if isinstance(storage, SequentialFile):
            return storage.read(rid)[1]
        return storage.read(rid)

    def _open_btree(self, table: str, col: Column,
                    create: bool = False) -> BPlusTree:
        return BPlusTree(
            self._index_path(table, col.name, "BTREE"),
            key_size(col),
            lambda v: encode_key(v, col),
            lambda b: decode_key(b, col),
            create=create,
            counter=self._counter,
        )

    def _open_hash(self, table: str, col: Column,
                   create: bool = False) -> ExtendibleHash:
        return ExtendibleHash(
            self._index_path(table, col.name, "HASH"),
            key_size(col),
            lambda v: encode_key(v, col),
            lambda b: decode_key(b, col),
            create=create,
            counter=self._counter,
        )

    def _open_rtree(self, table: str, col: Column,
                    create: bool = False) -> RTree:
        return RTree(self._index_path(table, col.name, "RTREE"),
                     create=create, counter=self._counter)

    # ------------------------------------------------------------------
    # Índice invertido (módulo textual)
    # ------------------------------------------------------------------
    def _open_inverted(self, table: str, col: Column,
                       create: bool = False) -> InvertedIndex:
        base = os.path.join(self.data_dir, f"{table}_{col.name}")
        return InvertedIndex(base, create=create, counter=self._counter)

    def _inverted_params(self, table: str, col_name: str) -> dict:
        """Parámetros WITH del índice INVERTED de la columna."""
        meta = self.catalog.index_on(table, col_name, {"INVERTED"})
        return (meta or {}).get("params") or {}

    @staticmethod
    def _preprocessor(params: dict) -> TextPreprocessor:
        """Preprocesador del índice (idioma de sus parámetros WITH)."""
        return TextPreprocessor(params.get("language", "spanish"))

    # ------------------------------------------------------------------
    # Índice vectorial IVF
    # ------------------------------------------------------------------
    def _open_ivf(self, table: str, col: Column,
                  create: bool = False) -> IVFIndex:
        base = os.path.join(self.data_dir, f"{table}_{col.name}")
        return IVFIndex(base, create=create, counter=self._counter)

    def _ivf_params(self, table: str, col_name: str) -> dict:
        """Parámetros WITH del índice IVF de la columna."""
        meta = self.catalog.index_on(table, col_name, {"IVF"})
        return (meta or {}).get("params") or {}

    # ------------------------------------------------------------------
    # Entrada principal
    # ------------------------------------------------------------------
    def execute(self, sql: str) -> dict:
        """Ejecuta una sentencia SQL y devuelve el resultado del contrato."""
        start = time.perf_counter()
        plan = _Plan()
        # Contador de I/O fresco por consulta: las estructuras abiertas
        # durante el dispatch envuelven sus archivos con CountedFile.
        self._counter = DiskCounter()
        try:
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
            finally:
                self._flush_seq()

            result["ok"] = True
            result["plan"] = plan.steps
            result["elapsed_ms"] = _ms(start)
            result["io"] = self._counter.snapshot()
            return result
        finally:
            self._counter = None

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
        if kind == "set":
            return self._exec_set(ast, plan)
        raise ExecutionError(f"sentencia no soportada: {kind}")

    # ------------------------------------------------------------------
    # SET parametro = valor (sesión)
    # ------------------------------------------------------------------
    def _exec_set(self, ast: dict, plan: _Plan) -> dict:
        """Aplica un parámetro de sesión (``SET ivf.probes = 5``)."""
        t = time.perf_counter()
        param = ast["param"]
        if param not in ("ivf.probes",):
            raise SemanticError(
                f"parámetro desconocido: {param!r} (disponible: ivf.probes)")
        self._session[param] = ast["value"]
        plan.add("Set Session Param", f"{param} = {ast['value']}", t)
        return {"kind": "set",
                "message": f"Parámetro {param} = {ast['value']}"}

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
        if self._is_sequential(table):
            paths = list(self._seq_paths(table))
        else:
            paths = [self._heap_path(table)]
        for meta in self.catalog.indexes(table):
            paths.extend(self._index_paths(table, meta["column"],
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
        return {"kind": "drop_table", "table": table,
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
    # Secuencia de la PK implícita (SERIAL)
    # ------------------------------------------------------------------
    def _next_auto_id(self, table: str) -> int:
        """Asigna el siguiente entero de la columna autogenerada de la tabla."""
        v = self._auto_cache.get(table, self.catalog.auto_seq(table))
        self._auto_cache[table] = v + 1
        self.catalog.set_auto_seq(table, v + 1)
        self._seq_dirty.add(table)
        return v

    def _bump_auto_seq(self, table: str, value: int) -> None:
        """Adelanta la secuencia cuando se inserta un id explícito mayor."""
        cur = self._auto_cache.get(table, self.catalog.auto_seq(table))
        if value >= cur:
            self._auto_cache[table] = value + 1
            self.catalog.set_auto_seq(table, value + 1)
            self._seq_dirty.add(table)

    def _flush_seq(self) -> None:
        if self._seq_dirty:
            self.catalog.save()
            self._seq_dirty.clear()

    @staticmethod
    def _with_implicit_pk(columns: list[Column]) -> list[Column]:
        """Agrega una PK autoincremental (SERIAL) si no se declaró ninguna.

        La columna se llama ``id`` salvo que ya exista una columna con ese
        nombre, en cuyo caso usa ``_minidb_id``. Se antepone al esquema.
        """
        if any(c.primary_key for c in columns):
            return columns
        name = "id" if "id" not in {c.name for c in columns} else "_minidb_id"
        return [Column(name, TYPE_INT, primary_key=True, auto=True)] + columns

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
        columns = self._with_implicit_pk(columns)
        auto_pk = next((c.name for c in columns if c.auto), None)
        organization = ast.get("organization", "heap")
        pk_col = next(c for c in columns if c.primary_key)
        if pk_col.type == TYPE_VECTOR:
            raise SemanticError(
                "una columna VECTOR(n) no puede ser PRIMARY KEY")
        if organization == "sequential" and pk_col.type == TYPE_POINT:
            raise SemanticError(
                "la PK de una tabla SEQUENTIAL no puede ser POINT")
        plan.add("Semantic Check", f"esquema de '{table}' válido"
                 + (f" (PK implícita {auto_pk} SERIAL)"
                    if auto_pk else "")
                 + (f", organización {organization.upper()}"
                    if organization != "heap" else ""), t)

        t = time.perf_counter()
        if organization == "sequential":
            storage = self._open_sequential(table, pk_col, create=True)
            storage.close()
            plan.add("Create Sequential File",
                     os.path.basename(self._seq_paths(table)[0]), t)
        else:
            heap = self._open_heap(table, create=True)
            heap.close()
            plan.add("Create Heap File",
                     os.path.basename(self._heap_path(table)), t)

        t = time.perf_counter()
        self.catalog.create_table(table, columns, organization=organization)
        plan.add("Update Catalog", "catalog.json persistido", t)
        self._create_pk_index(table, columns, plan)
        return {"kind": "create_table", "table": table,
                "message": f"Tabla '{table}' creada"}

    def _create_pk_index(self, table: str, columns: list[Column],
                         plan: _Plan) -> None:
        """Crea automáticamente un B+ Tree para la PRIMARY KEY.

        Igual que PostgreSQL, toda PK lleva su índice; además evita que la
        verificación de unicidad en INSERT degrade a un escaneo O(n) por fila.
        Excepción: en tablas SEQUENTIAL el propio archivo ordenado es la vía
        de acceso de la PK, así que no se genera índice.
        """
        pk = next((c for c in columns if c.primary_key), None)
        if pk is None or pk.type in (TYPE_POINT, TYPE_VECTOR):
            return
        if self.catalog.organization(table) == "sequential":
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
        """Lee un CSV de ``CSV_DIR`` (defecto ``./data``)."""
        try:
            path = resolve_dataset_path(
                os.environ.get("CSV_DIR", "./data"), filename)
        except ValueError as exc:
            raise SemanticError(str(exc)) from exc
        if not os.path.isfile(path):
            raise ExecutionError(
                f"el archivo '{filename}' no existe en CSV_DIR")
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
        raw_rows = [r for _, r in rows]
        columns = infer_columns(header, raw_rows)
        det = detect_point_pair(header, columns, raw_rows)
        if det is not None:
            columns.append(Column(det["column"], TYPE_POINT))
        columns = self._with_implicit_pk(columns)
        pk = next((c.name for c in columns if c.primary_key), None)
        auto_pk = next((c.name for c in columns if c.auto), None)
        plan.add(
            "Infer Schema",
            f"{len(columns)} columnas inferidas de '{filename}'"
            + (f", PK sugerida: {pk}" if pk else "")
            + (f" (implícita, autogenerada)" if auto_pk else "")
            + (f", {det['column']} POINT derivada de "
               f"{det['lat_col']}+{det['lng_col']}" if det else ""), t)

        t = time.perf_counter()
        heap = self._open_heap(table, create=True)
        heap.close()
        plan.add("Create Heap File", os.path.basename(self._heap_path(table)), t)

        t = time.perf_counter()
        self.catalog.create_table(table, columns)
        plan.add("Update Catalog", "catalog.json persistido", t)
        self._create_pk_index(table, columns, plan)

        t = time.perf_counter()
        # La columna autogenerada (PK implícita) no viene en el CSV: se
        # antepone una celda vacía a cada fila y se autoasigna al cargar.
        pad = [""] if auto_pk is not None else []
        load_rows: list[tuple[int, list[str]]] = [
            (ln, pad + raw) for ln, raw in rows]
        derive: tuple[int, str] | None = None
        if det is not None:
            lat_i = column_position(header, det["lat_col"])
            lng_i = column_position(header, det["lng_col"])
            load_rows = [
                (ln, list(raw) + [
                    raw[len(pad) + lat_i] if len(pad) + lat_i < len(raw) else "",
                    raw[len(pad) + lng_i] if len(pad) + lng_i < len(raw) else ""])
                for ln, raw in load_rows
            ]
            derive = (len(columns) - 1, det["column"])
        stats = self.bulk_load_rows(table, load_rows, derive=derive)
        plan.add("Bulk Load",
                 f"{stats['rows_loaded']} filas cargadas, "
                 f"{stats['rows_rejected']} rechazadas desde '{filename}'", t)
        return {"kind": "create_table", "table": table,
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
        raw_rows = [r for _, r in rows]
        # Derivación automática: la tabla tiene exactamente una columna
        # POINT ausente del CSV y el CSV trae un par lat/lng detectable.
        point_col: Column | None = None
        det: dict | None = None
        point_cols = [c for c in columns if c.type == TYPE_POINT]
        header_names = {h.strip().lower() for h in header}
        if (len(point_cols) == 1
                and point_cols[0].name.lower() not in header_names):
            det = detect_point_pair(
                header, infer_columns(header, raw_rows), raw_rows)
            if det is not None:
                point_col = point_cols[0]
        try:
            auto_names = {c.name for c in columns if c.auto}
            positions, ignored = map_columns(
                header,
                [c for c in columns if c is not point_col]
                if point_col is not None else columns,
                optional=auto_names)
        except ValueError as exc:
            raise SemanticError(str(exc)) from exc
        plan.add("Map Columns",
                 f"{len(positions)} columnas mapeadas por nombre"
                 + (f", ignoradas: {', '.join(ignored)}" if ignored else "")
                 + (f", {point_col.name} derivada de "
                    f"{det['lat_col']}+{det['lng_col']}"
                    if point_col is not None else ""), t)

        t = time.perf_counter()
        derive: tuple[int, str] | None = None
        if point_col is not None:
            lat_i = column_position(header, det["lat_col"])
            lng_i = column_position(header, det["lng_col"])
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
        stats = self.bulk_load_rows(table, mapped, derive=derive)
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
        params = ast.get("params") or {}
        t = time.perf_counter()
        self._get_columns(table)
        col = self._get_column(table, col_name)
        if itype == "RTREE":
            if col.type != TYPE_POINT:
                raise SemanticError("RTREE solo se permite sobre columnas POINT")
        elif itype == "INVERTED":
            if col.type not in (TYPE_TEXT, TYPE_VARCHAR):
                raise SemanticError(
                    "INVERTED solo se permite sobre columnas TEXT o VARCHAR")
            scoring = str(params.get("scoring", "TFIDF_COSINE")).upper()
            if scoring not in ("TFIDF_COSINE",):
                raise SemanticError(
                    f"scoring no soportado: {scoring!r} "
                    f"(disponible: TFIDF_COSINE)")
            params["scoring"] = scoring
            language = str(params.get("language", "spanish")).lower()
            try:
                TextPreprocessor(language)
            except ValueError as exc:
                raise SemanticError(str(exc)) from exc
            params["language"] = language
        elif itype == "IVF":
            if col.type != TYPE_VECTOR:
                raise SemanticError(
                    "IVF solo se permite sobre columnas VECTOR(n)")
            metric = str(params.get("metric", "L2")).upper()
            if metric not in IVF_METRICS:
                raise SemanticError(
                    f"métrica no soportada: {metric!r} "
                    f"(disponibles: {', '.join(IVF_METRICS)})")
            params["metric"] = metric
            if "lists" in params:
                lists = params["lists"]
                if not isinstance(lists, int) or lists <= 0:
                    raise SemanticError(
                        "el parámetro lists requiere un entero positivo")
        elif col.type == TYPE_POINT:
            raise SemanticError(f"{itype} no soporta columnas POINT")
        elif col.type == TYPE_VECTOR:
            raise SemanticError(f"{itype} no soporta columnas VECTOR "
                                f"(usar IVF)")
        if self.catalog.index_on(table, col_name, {itype}):
            raise SemanticError(
                f"ya existe un índice {itype} sobre {table}.{col_name}")
        pk = self.catalog.primary_key(table)
        if (self._is_sequential(table) and pk is not None
                and col_name == pk.name and itype in ("BTREE", "HASH")):
            raise SemanticError(
                f"la PK de una tabla SEQUENTIAL ya es la vía de acceso "
                f"ordenada: no se permite CREATE INDEX {itype} sobre "
                f"{table}.{col_name}")
        idx_name = ast["name"] or f"{table}_{col_name}_{itype.lower()}"
        plan.add("Semantic Check",
                 f"{itype} sobre {table}.{col_name} es válido", t)

        t = time.perf_counter()
        heap = self._open_storage(table)
        if itype == "INVERTED":
            idx = self._open_inverted(table, col, create=True)
            pre = self._preprocessor(params)
            plan.add("Create Index File",
                     ", ".join(os.path.basename(p)
                               for p in self._index_paths(table, col_name,
                                                          itype)), t)
            t = time.perf_counter()
            col_pos = [c.name for c in self.catalog.columns(table)
                       ].index(col_name)
            n = 0
            for rid, raw in self._scan_rows(heap):
                row = deserialize_row(self.catalog.columns(table), raw)
                idx.add_document(rid, pre.terms(str(row[col_pos])))
                n += 1
            idx.flush()
            idx.close()
            heap.close()
            plan.add("Bulk Load",
                     f"{n} documentos indexados con INVERTED "
                     f"(idioma {params['language']})", t)
        elif itype == "IVF":
            t = time.perf_counter()
            col_pos = [c.name for c in self.catalog.columns(table)
                       ].index(col_name)
            vectors = []
            for rid, raw in self._scan_rows(heap):
                row = deserialize_row(self.catalog.columns(table), raw)
                vectors.append((rid, tuple(row[col_pos])))
            heap.close()
            n = len(vectors)
            if n == 0:
                raise SemanticError(
                    f"no se puede crear un índice IVF: la tabla "
                    f"'{table}' está vacía")
            lists = params.get("lists") or max(1, int(math.floor(
                math.sqrt(n))))
            params["lists"] = lists
            idx = self._open_ivf(table, col, create=True)
            plan.add("Create Index File",
                     ", ".join(os.path.basename(p)
                               for p in self._index_paths(table, col_name,
                                                          itype)), t)
            t = time.perf_counter()
            idx.build(vectors, dim=col.size, n_clusters=lists,
                      metric=params["metric"])
            idx.close()
            plan.add("K-Means + Bulk Load",
                     f"{n} vectores en {lists} celdas "
                     f"(métrica {params['metric']})", t)
        else:
            if itype == "BTREE":
                idx = self._open_btree(table, col, create=True)
            elif itype == "HASH":
                idx = self._open_hash(table, col, create=True)
            else:
                idx = self._open_rtree(table, col, create=True)
            plan.add("Create Index File",
                     os.path.basename(
                         self._index_path(table, col_name, itype)), t)

            t = time.perf_counter()
            col_pos = [c.name for c in self.catalog.columns(table)
                       ].index(col_name)
            if hasattr(idx, "defer_header"):  # BPlusTree/RTree
                idx.defer_header = True
            if hasattr(idx, "defer_flush"):  # BPlusTree/RTree/ExtendibleHash
                idx.defer_flush = True
            n = 0
            for rid, raw in self._scan_rows(heap):
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
        self.catalog.add_index(table, idx_name, col_name, itype, params)
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
        values = list(ast["values"])
        auto_pos = next((i for i, c in enumerate(columns) if c.auto), None)
        if auto_pos is not None and len(values) == len(columns) - 1:
            # INSERT sin mencionar la columna autogenerada: se autoasigna.
            values.insert(auto_pos, None)
        if len(values) != len(columns):
            raise SemanticError(
                f"'{table}' tiene {len(columns)} columnas, "
                f"se recibieron {len(ast['values'])} valores")
        row = [None if v is None and c.auto else coerce_value(v, c)
               for v, c in zip(values, columns)]
        plan.add("Semantic Check", f"tipos válidos para '{table}'", t)

        self._insert_row(table, columns, row, plan)
        return {"kind": "insert", "rowcount": 1, "message": "1 registro insertado"}

    def _insert_row(self, table: str, columns: list[Column], row: list,
                    plan: _Plan | None = None) -> RID:
        """Inserta una fila validada manteniendo el archivo de datos e índices.

        Verifica la unicidad de la PRIMARY KEY (índice si existe, búsqueda
        binaria en tablas SEQUENTIAL, si no escaneo secuencial). La columna
        ``auto`` (PK implícita SERIAL) recibe un entero creciente cuando el
        valor es ``None``; un valor explícito se respeta y adelanta la
        secuencia. Devuelve el RID.
        """
        pk = self.catalog.primary_key(table)
        if pk is not None:
            t = time.perf_counter()
            pk_pos = [c.name for c in columns].index(pk.name)
            if pk.auto:
                if row[pk_pos] is None:
                    row[pk_pos] = self._next_auto_id(table)
                else:
                    self._bump_auto_seq(table, row[pk_pos])
            value = row[pk_pos]
            if self._is_sequential(table):
                return self._insert_row_sequential(
                    table, columns, row, pk, value, plan)
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
                heap = self._open_storage(table)
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
        heap = self._open_storage(table)
        rid = heap.insert(serialize_row(columns, row))
        heap.close()
        if plan is not None:
            plan.add("Heap Insert", f"RID = ({rid[0]}, {rid[1]})", t)

        self._index_insert_all(table, columns, row, rid, plan)
        return rid

    def _insert_row_sequential(self, table: str, columns: list[Column],
                               row: list, pk: Column, value,
                               plan: _Plan | None) -> RID:
        """INSERT en tabla SEQUENTIAL: unicidad por búsqueda binaria e
        inserción en el archivo ordenado (overflow + reenlace)."""
        t = time.perf_counter()
        storage = self._open_storage(table)
        if storage.search(value) is not None:
            storage.close()
            raise ExecutionError(
                f"clave primaria duplicada: {pk.name} = {value!r}")
        if plan is not None:
            plan.add("Binary Search (Sequential)",
                     f"unicidad de {table}.{pk.name}", t)
        t = time.perf_counter()
        rid = storage.insert(value, serialize_row(columns, row))
        storage.close()
        if plan is not None:
            plan.add("Sequential Insert", f"RID = ({rid[0]}, {rid[1]})", t)
        self._index_insert_all(table, columns, row, rid, plan)
        return rid

    def _index_insert_all(self, table: str, columns: list[Column], row: list,
                          rid: RID, plan: _Plan | None) -> None:
        """Mantiene los índices secundarios tras insertar una fila."""
        for meta in self.catalog.indexes(table):
            t = time.perf_counter()
            col = self._get_column(table, meta["column"])
            col_pos = [c.name for c in columns].index(col.name)
            value = row[col_pos]
            if meta["type"] == "INVERTED":
                idx = self._open_inverted(table, col)
                pre = self._preprocessor(
                    self._inverted_params(table, col.name))
                idx.add_document(rid, pre.terms(str(value)))
            elif meta["type"] == "IVF":
                idx = self._open_ivf(table, col)
                idx.add(rid, tuple(value))
            else:
                if meta["type"] == "BTREE":
                    idx = self._open_btree(table, col)
                elif meta["type"] == "HASH":
                    idx = self._open_hash(table, col)
                else:
                    idx = self._open_rtree(table, col)
                idx.insert(tuple(value) if col.type == TYPE_POINT else value,
                           rid)
            idx.close()
            if plan is not None:
                plan.add("Index Maintenance",
                         f"{meta['type']} ON {table}.{col.name} actualizado", t)

    def _open_index(self, table: str, itype: str, col: Column):
        """Abre el índice ``itype`` de ``table`` sobre la columna ``col``."""
        if itype == "BTREE":
            return self._open_btree(table, col)
        if itype == "HASH":
            return self._open_hash(table, col)
        if itype == "INVERTED":
            return self._open_inverted(table, col)
        if itype == "IVF":
            return self._open_ivf(table, col)
        return self._open_rtree(table, col)

    def bulk_load_rows(self, table: str,
                       rows: list[tuple[int, list[str]]],
                       derive: tuple[int, str] | None = None) -> dict:
        """Carga masiva de filas crudas (strings) en una tabla existente.

        Cada elemento es ``(número de línea, valores crudos)`` ya
        ordenados según las columnas de la tabla. Las filas inválidas se
        rechazan sin abortar la carga; se conservan hasta ``MAX_ERRORS``
        errores con su número de línea.

        Con ``derive = (posicion, nombre)`` la fila trae dos valores crudos
        EXTRA al final (latitud y longitud): se construye el punto
        ``(lat, lng)`` y se inserta en ``posicion`` antes del casteo; si
        no son numéricos la fila se rechaza como ``lat/lng inválidos``.

        El heap file y todos los índices se abren una sola vez y con el
        volcado de cabecera diferido (se persiste todo al cerrar), en vez
        de abrir/cerrar y reescribir cabeceras por fila. La unicidad de
        la PRIMARY KEY se verifica igual que en ``_insert_row`` (índice
        o escaneo por fila); en cargas grandes se siembra un conjunto
        con las claves existentes, lo cual es equivalente: contiene
        exactamente los mismos valores que encontraría el índice.
        """
        columns = self._get_columns(table)
        loaded = rejected = 0
        errors: list[dict] = []
        expected = len(columns) + (1 if derive else 0)
        pk = self.catalog.primary_key(table)
        pk_pos = ([c.name for c in columns].index(pk.name)
                  if pk is not None else None)

        heap = self._open_storage(table)
        heap.defer_header = True
        heap.defer_flush = True
        sequential = isinstance(heap, SequentialFile)
        col_names = [c.name for c in columns]
        idx_entries: list[tuple[dict, Column, int, object, object]] = []
        for meta in self.catalog.indexes(table):
            col = self.catalog.column(table, meta["column"])
            idx = self._open_index(table, meta["type"], col)
            if hasattr(idx, "defer_header"):  # BPlusTree/RTree
                idx.defer_header = True
            if hasattr(idx, "defer_flush"):
                idx.defer_flush = True  # write-back durante toda la carga
            pre = (self._preprocessor(meta.get("params") or {})
                   if meta["type"] == "INVERTED" else None)
            idx_entries.append(
                (meta, col, col_names.index(col.name), idx, pre))
        pk_idx = next(
            (e for e in idx_entries
             if pk is not None and e[1].name == pk.name
             and e[0]["type"] in ("BTREE", "HASH")), None)

        pk_seen: set | None = None
        if pk is not None and len(rows) >= _BULK_PK_SET_MIN:
            pk_seen = set()
            if sequential:
                for _rid, key, _raw in heap.scan():
                    pk_seen.add(key)
            elif pk_idx is not None and pk_idx[0]["type"] == "BTREE":
                for key, _rid in pk_idx[3].range_search():
                    pk_seen.add(key)
            else:  # HASH sin recorrido, o PK sin índice: una pasada al heap
                for _rid, raw in heap.scan():
                    pk_seen.add(deserialize_row(columns, raw)[pk_pos])

        try:
            for line_no, raw_row in rows:
                if len(raw_row) != expected:
                    rejected += 1
                    if len(errors) < MAX_ERRORS:
                        errors.append({
                            "line": line_no,
                            "reason": f"se esperaban {expected} valores, "
                                      f"llegaron {len(raw_row)}",
                        })
                    continue
                if derive is not None:
                    point_pos, point_name = derive
                    try:
                        point = derive_point_value(raw_row[-2], raw_row[-1],
                                                   point_name, line_no)
                    except ValueError as exc:
                        rejected += 1
                        if len(errors) < MAX_ERRORS:
                            errors.append({"line": line_no,
                                           "reason": str(exc)})
                        continue
                    raw_row = list(raw_row[:-2])
                    raw_row.insert(point_pos, point)
                try:
                    row = [None if c.auto and v.strip() == ""
                           else cast_csv_value(v, c)
                           for v, c in zip(raw_row, columns)]
                    if pk is not None:
                        # Mismo orden que _insert_row: asignar/adelantar
                        # la secuencia ANTES de verificar la unicidad.
                        if pk.auto:
                            if row[pk_pos] is None:
                                row[pk_pos] = self._next_auto_id(table)
                            else:
                                self._bump_auto_seq(table, row[pk_pos])
                        value = row[pk_pos]
                        if pk_seen is not None:
                            duplicate = value in pk_seen
                        elif sequential:
                            duplicate = heap.search(value) is not None
                        elif pk_idx is not None:
                            duplicate = bool(pk_idx[3].search(value))
                        else:
                            duplicate = any(
                                deserialize_row(columns, raw)[pk_pos] == value
                                for _r, raw in heap.scan())
                        if duplicate:
                            raise ExecutionError(
                                f"clave primaria duplicada: "
                                f"{pk.name} = {value!r}")
                        if pk_seen is not None:
                            pk_seen.add(value)
                    if sequential:
                        rid = heap.insert(row[pk_pos],
                                          serialize_row(columns, row))
                    else:
                        rid = heap.insert(serialize_row(columns, row))
                    for _meta, col, col_pos, idx, pre in idx_entries:
                        v = row[col_pos]
                        if _meta["type"] == "INVERTED":
                            idx.add_document(rid, pre.terms(str(v)))
                        elif _meta["type"] == "IVF":
                            idx.add(rid, tuple(v))
                        else:
                            idx.insert(tuple(v) if col.type == TYPE_POINT
                                       else v, rid)
                except (ValueError, SerializationError,
                        ExecutionError) as exc:
                    rejected += 1
                    if len(errors) < MAX_ERRORS:
                        errors.append({"line": line_no,
                                       "reason": str(exc)})
                    continue
                loaded += 1
        finally:
            heap.close()
            for _meta, _col, _pos, idx, _pre in idx_entries:
                idx.close()
        self._flush_seq()
        return {"rows_loaded": loaded, "rows_rejected": rejected,
                "errors": errors}

    # ------------------------------------------------------------------
    # SELECT
    # ------------------------------------------------------------------
    def _exec_select(self, ast: dict, plan: _Plan) -> dict:
        items = ast["columns"]
        if any(isinstance(c, dict) and c["agg"] is not None for c in items):
            return self._exec_aggregate_select(ast, plan)
        table = ast["table"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        col_map = {c.name: c for c in columns}
        # Modo enriquecido: items como diccionarios (alias AS y/o
        # COSINE_SCORE()). `selected` lleva solo columnas reales; el
        # puntaje se proyecta aparte al final de cada fila.
        score_item: dict | None = None
        vdist_item: dict | None = None
        vdist_alias: str | None = None
        aliases: dict[str, str] = {}
        if items == ["*"]:
            selected = [c.name for c in columns]
        elif any(isinstance(c, dict) for c in items):
            selected = []
            for it in items:
                if it.get("score"):
                    if score_item is not None:
                        raise SemanticError(
                            "solo se permite un COSINE_SCORE() por consulta")
                    score_item = it
                    continue
                if it.get("vdist"):
                    if vdist_item is not None:
                        raise SemanticError(
                            "solo se permite una distancia vectorial "
                            "por consulta")
                    vdist_item = it["vdist"]
                    vdist_alias = it.get("alias")
                    continue
                if it["column"] not in col_map:
                    raise SemanticError(
                        f"la columna '{it['column']}' no existe en '{table}'")
                selected.append(it["column"])
                if it.get("alias"):
                    aliases[it["column"]] = it["alias"]
        else:
            for name in items:
                if name not in col_map:
                    raise SemanticError(
                        f"la columna '{name}' no existe en '{table}'")
            selected = items
        where = ast["where"]
        if where is not None:
            self._check_where_columns(table, where)
        match_cond = self._match_condition(where)
        if score_item is not None and match_cond is None:
            raise SemanticError(
                "COSINE_SCORE() requiere una condición MATCH en el WHERE")
        if vdist_item is not None:
            if score_item is not None:
                raise SemanticError(
                    "no se puede combinar COSINE_SCORE() con una "
                    "distancia vectorial en la misma consulta")
            return self._exec_vector_select(
                ast, plan, columns, selected, aliases,
                vdist_item, vdist_alias)
        if ast.get("order_by") and score_item is None \
                and self._knn_condition(where) is None:
            raise SemanticError(
                "ORDER BY solo se soporta en consultas con ranking "
                "(COSINE_SCORE o KNN)")
        # La condición MATCH se anota como "ranked" cuando la consulta pide
        # COSINE_SCORE(): deja de ser un predicado booleano y pasa a ser un
        # ranking Top-K (igual que KNN). La anotación viaja en el propio
        # árbol WHERE hacia _plan_access y _match.
        if match_cond is not None:
            match_cond["ranked"] = score_item is not None
        plan.add("Semantic Check", f"tabla '{table}' y columnas válidas", t)

        # Acceso a datos: índice si existe, si no escaneo secuencial
        limit = ast["limit"]
        offset = ast["offset"] or 0
        limit_hint = (offset + limit) if limit is not None else SELECT_ROW_CAP
        rids: list[RID] | None = None  # None => escaneo secuencial
        ordered_knn: list[tuple[RID, float]] | None = None
        residual: dict | None = None  # condiciones del AND fuera del acceso
        if where is not None:
            rids, ordered_knn, residual = self._plan_access(
                table, where, plan, limit_hint=limit_hint)
        knn_cond = self._knn_condition(where)

        t = time.perf_counter()
        heap = self._open_storage(table)
        # Semántica SQL estándar: saltar las primeras `offset` filas del
        # resultado y luego aplicar el límite. Con LIMIT explícito el
        # escaneo junta offset+limit filas que cumplen (pushdown); sin
        # LIMIT explícito aplica el tope SELECT_ROW_CAP y sondea una fila
        # extra para saber si el resultado total lo supera (truncated).
        if limit is not None:
            stop_at = offset + limit
        else:
            stop_at = SELECT_ROW_CAP + 1  # +1: sondeo para detectar truncated
        truncated = False
        # MATCH con ranking sin índice INVERTED: ranking TF-IDF por fuerza
        # bruta sobre el escaneo completo (línea base del Experimento 6).
        ranked_scan = (score_item is not None and rids is None
                       and match_cond is not None)
        score_map: dict[RID, float] = {}
        if rids is None:  # escaneo secuencial
            # El LIMIT se empuja al escaneo: se detiene apenas junta las
            # filas pedidas (salvo KNN o MATCH con ranking por fuerza
            # bruta, que necesitan todas las filas para ordenar).
            early_stop = knn_cond is None and not ranked_scan
            rows = []
            for rid, raw in self._scan_rows(heap):
                row = deserialize_row(columns, raw)
                if where is not None and not self._match(
                        columns, where, row, table):
                    continue
                rows.append((rid, row))
                if early_stop and len(rows) >= stop_at:
                    break
            fetched = len(rows)  # registros leídos antes de la ventana
            if knn_cond is not None:
                # KNN por fuerza bruta cuando no hay R-Tree
                pos = [c.name for c in columns].index(knn_cond["column"])
                cx, cy = knn_cond["center"]
                rows.sort(key=lambda t: math.hypot(t[1][pos][0] - cx,
                                                   t[1][pos][1] - cy))
                rows = rows[: knn_cond["k"]]
            elif ranked_scan:
                scored_rows = self._fulltext_rank(
                    table, columns, rows, match_cond, stop_at)
                score_map = {rid: s for rid, _row, s in scored_rows}
                rows = [(rid, row) for rid, row, _s in scored_rows]
            # OFFSET/LIMIT (o el tope de seguridad) sobre el resultado.
            # (Con ranking la ventana se aplica tras la proyección y el
            # ORDER BY.)
            if not ranked_scan:
                if limit is not None:
                    rows = rows[offset:offset + limit]
                else:
                    truncated = len(rows) > SELECT_ROW_CAP
                    rows = rows[:SELECT_ROW_CAP]
        elif ordered_knn is not None:
            if score_item is not None:
                score_map = {rid: s for rid, s in ordered_knn}
            rows = []
            for rid, _d in ordered_knn:
                row = deserialize_row(columns, self._read_row(heap, rid))
                if residual is not None and not self._match(
                        columns, residual, row, table):
                    continue
                rows.append((rid, row))
            fetched = len(rows)
            # offset/limit se aplica sobre out_rows tras la proyección.
        elif residual is not None:
            # El acceso por índice solo garantiza la condición driver: el
            # resto del AND filtra aquí, con parada temprana en stop_at.
            rows = []
            fetched = 0
            for rid in rids:
                row = deserialize_row(columns, self._read_row(heap, rid))
                fetched += 1
                if not self._match(columns, residual, row, table):
                    continue
                rows.append((rid, row))
                if len(rows) >= stop_at:
                    break
            if limit is not None:
                rows = rows[offset:offset + limit]
            else:
                truncated = len(rows) > SELECT_ROW_CAP
                rows = rows[:SELECT_ROW_CAP]
        else:
            if limit is not None:
                # pushdown: los RIDs ya cumplen el WHERE
                rids = rids[offset:offset + limit]
            else:
                truncated = len(rids) > SELECT_ROW_CAP
                rids = rids[:SELECT_ROW_CAP]
            fetched = len(rids)
            rows = [(rid, deserialize_row(columns, self._read_row(heap, rid)))
                    for rid in rids]
        heap.close()
        plan.add("Fetch Rows", f"{fetched} registros leídos del heap", t)

        t = time.perf_counter()
        positions = [[c.name for c in columns].index(name) for name in selected]
        out_rows = [[row[i] for i in positions] for _, row in rows]
        out_names = [aliases.get(n, n) for n in selected]
        if score_item is not None:
            for out_r, (rid, _row) in zip(out_rows, rows):
                out_r.append(round(score_map.get(rid, 0.0), 6))
            out_names.append(score_item.get("alias") or "cosine_score")
        if ordered_knn is not None or ranked_scan:
            # ORDER BY sobre el resultado ordenado (ranking), antes de la
            # ventana OFFSET/LIMIT (semántica SQL estándar).
            if ast.get("order_by"):
                ob = ast["order_by"]
                if ob not in out_names:
                    raise SemanticError(
                        f"ORDER BY: la columna '{ob}' no está en el SELECT")
                oi = out_names.index(ob)
                out_rows.sort(key=lambda r: (r[oi] is None, r[oi]),
                              reverse=ast["order_dir"] == "desc")
            if limit is not None:
                out_rows = out_rows[offset:offset + limit]
            elif len(out_rows) > SELECT_ROW_CAP:
                out_rows = out_rows[:SELECT_ROW_CAP]
                truncated = True
        plan.add("Projection", f"columnas: {', '.join(out_names)}", t)
        if limit is not None:
            t = time.perf_counter()
            detail = f"máximo {limit} filas"
            if offset:
                detail += f", salta {offset}"
            plan.add("Limit", detail, t)

        spatial = self._spatial_payload(columns, selected, out_rows)
        return {
            "kind": "select",
            "columns": out_names,
            "rows": [[list(v) if isinstance(v, tuple) else v for v in row]
                     for row in out_rows],
            "rowcount": len(out_rows),
            "message": "OK",
            "spatial": spatial,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # SELECT con distancia vectorial (embedding <-> / <=> 'query')
    # ------------------------------------------------------------------
    def _exec_vector_select(self, ast: dict, plan: _Plan,
                            columns: list[Column], selected: list[str],
                            aliases: dict[str, str], vdist: dict,
                            vdist_alias: str | None) -> dict:
        """Búsqueda por similitud de embeddings con planificador híbrido.

        Estrategia (reportada en el plan, como exige el enunciado):

        - **Pre-filtering**: si el WHERE tiene vía de acceso por índice
          relacional (B+/Hash/R-Tree), se recuperan esos RIDs y se
          calcula la distancia exacta solo sobre ellos.
        - **Post-filtering**: si no hay índice relacional pero sí IVF,
          el ANN devuelve candidatos y el WHERE filtra después.
        - **ANN puro**: sin WHERE, Top-K directo del IVF (probes del
          parámetro de sesión ``ivf.probes``, por defecto 1).
        - **Exact Vector Scan**: sin ningún índice, k-NN lineal por
          fuerza bruta (línea base / ground truth del Experimento 8).
        """
        table = ast["table"]
        t = time.perf_counter()
        col = self._get_column(table, vdist["column"])
        if col.type != TYPE_VECTOR:
            raise SemanticError(
                f"el operador {vdist['op']} requiere una columna "
                f"VECTOR(n), '{col.name}' es {col.type_str()}")
        query = coerce_vector(vdist["query"], col)
        metric = "COSINE" if vdist["op"] == "<=>" else "L2"
        where = ast["where"]
        if where is not None:
            self._check_where_columns(table, where)
        plan.add("Semantic Check",
                 f"distancia {metric} sobre {table}.{col.name}", t)

        limit = ast["limit"]
        offset = ast["offset"] or 0
        k = (offset + limit) if limit is not None else SELECT_ROW_CAP
        pos = [c.name for c in columns].index(col.name)

        ivf_meta = self.catalog.index_on(table, col.name, {"IVF"})
        if ivf_meta is not None:
            idx_metric = (ivf_meta.get("params") or {}).get("metric", "L2")
            if idx_metric != metric:
                raise SemanticError(
                    f"el índice IVF de {table}.{col.name} es {idx_metric}: "
                    f"usar el operador "
                    f"{'<=>' if idx_metric == 'COSINE' else '<->'} "
                    f"o recrear el índice con metric = '{metric}'")

        scored: list[tuple[RID, list, float]]
        truncated = False
        t = time.perf_counter()
        heap = self._open_storage(table)
        if where is not None:
            rids, _ordered, residual = self._plan_access(table, where, plan)
            if rids is not None:
                # Pre-filtering: el índice relacional acota los candidatos.
                scored = []
                for rid in rids:
                    row = deserialize_row(columns, self._read_row(heap, rid))
                    if residual is not None and not self._match(
                            columns, residual, row, table):
                        continue
                    scored.append((rid, row,
                                   vector_distance(query, row[pos], metric)))
                scored.sort(key=lambda t2: t2[2])
                total = len(scored)
                scored = scored[:k]
                truncated = limit is None and total > SELECT_ROW_CAP
                strategy = ("Pre-filtering (índice relacional + "
                            "distancia exacta sobre los RIDs filtrados)")
            elif ivf_meta is not None:
                # Post-filtering: el ANN recupera candidatos y el WHERE
                # filtra después.
                probes = self._session.get("ivf.probes", 1)
                t2 = time.perf_counter()
                idx = self._open_ivf(table, col)
                ann = idx.search(query, probes, k)
                idx.close()
                plan.add("IVF ANN Scan",
                         f"USING IVF ON {table}.{col.name} probes={probes} "
                         f"-> {len(ann)} candidatos", t2)
                scored = []
                for rid, d in ann:
                    row = deserialize_row(columns, self._read_row(heap, rid))
                    if not self._match(columns, where, row, table):
                        continue
                    scored.append((rid, row, d))
                strategy = (f"Post-filtering (ANN con probes={probes} + "
                            f"filtro WHERE sobre candidatos)")
            else:
                scored = self._exact_vector_scan(
                    heap, columns, pos, query, metric, where, table)
                total = len(scored)
                scored = scored[:k]
                truncated = limit is None and total > SELECT_ROW_CAP
                strategy = "Exact Vector Scan (k-NN lineal por fuerza bruta)"
        elif ivf_meta is not None:
            probes = self._session.get("ivf.probes", 1)
            t2 = time.perf_counter()
            idx = self._open_ivf(table, col)
            ann = idx.search(query, probes, k)
            idx.close()
            plan.add("IVF ANN Scan",
                     f"USING IVF ON {table}.{col.name} probes={probes} "
                     f"-> {len(ann)} candidatos", t2)
            scored = [(rid,
                       deserialize_row(columns, self._read_row(heap, rid)), d)
                      for rid, d in ann]
            strategy = f"ANN puro (IVF con probes={probes}, sin WHERE)"
        else:
            scored = self._exact_vector_scan(
                heap, columns, pos, query, metric, None, table)
            total = len(scored)
            scored = scored[:k]
            truncated = limit is None and total > SELECT_ROW_CAP
            strategy = "Exact Vector Scan (k-NN lineal por fuerza bruta)"
        heap.close()
        plan.add("Fetch Rows", f"{len(scored)} candidatos evaluados", t)
        plan.add("Vector Strategy", strategy, t)

        t = time.perf_counter()
        positions = [[c.name for c in columns].index(name)
                     for name in selected]
        out_rows = [[row[i] for i in positions] + [round(d, 6)]
                    for _rid, row, d in scored]
        dist_name = vdist_alias or ("dist_coseno" if metric == "COSINE"
                                    else "dist_l2")
        out_names = [aliases.get(n, n) for n in selected] + [dist_name]
        # ORDER BY: por defecto la distancia ascendente; con ORDER BY se
        # ordena por la columna pedida antes de la ventana OFFSET/LIMIT.
        if ast.get("order_by"):
            ob = ast["order_by"]
            if ob not in out_names:
                raise SemanticError(
                    f"ORDER BY: la columna '{ob}' no está en el SELECT")
            oi = out_names.index(ob)
            out_rows.sort(key=lambda r: (r[oi] is None, r[oi]),
                          reverse=ast["order_dir"] == "desc")
        if limit is not None:
            out_rows = out_rows[offset:offset + limit]
        plan.add("Projection", f"columnas: {', '.join(out_names)}", t)
        return {
            "kind": "select",
            "columns": out_names,
            "rows": [[list(v) if isinstance(v, tuple) else v for v in row]
                     for row in out_rows],
            "rowcount": len(out_rows),
            "message": "OK",
            "spatial": None,
            "truncated": truncated,
        }

    def _exact_vector_scan(self, heap, columns: list[Column], pos: int,
                           query, metric: str, where: dict | None,
                           table: str) -> list[tuple[RID, list, float]]:
        """k-NN lineal exacto: distancia a todas las filas (que cumplan el
        WHERE) ordenada ascendente. Línea base del Experimento 8."""
        scored = []
        for rid, raw in self._scan_rows(heap):
            row = deserialize_row(columns, raw)
            if where is not None and not self._match(columns, where, row,
                                                     table):
                continue
            scored.append((rid, row, vector_distance(query, row[pos],
                                                     metric)))
        scored.sort(key=lambda t: t[2])
        return scored

    # ------------------------------------------------------------------
    # SELECT con agregación (un solo grupo, sin GROUP BY)
    # ------------------------------------------------------------------
    # Tipos soportados por cada función de agregación.
    _AGG_TYPES = {
        "min": {TYPE_INT, TYPE_FLOAT, TYPE_VARCHAR},
        "max": {TYPE_INT, TYPE_FLOAT, TYPE_VARCHAR},
        "sum": {TYPE_INT, TYPE_FLOAT},
        "avg": {TYPE_INT, TYPE_FLOAT},
    }

    def _exec_aggregate_select(self, ast: dict, plan: _Plan) -> dict:
        """Ejecuta SELECT con COUNT/MIN/MAX/SUM/AVG sobre un único grupo.

        El resultado es siempre una fila con tantas columnas como
        expresiones de agregación (``count``, ``min_lat``, ...). No se
        aplican LIMIT/OFFSET ni el tope SELECT_ROW_CAP: el agregado se
        calcula sobre todas las filas que cumplen el WHERE en un solo
        pase con acumuladores. Con cero filas coincidentes COUNT
        devuelve 0 y MIN/MAX/SUM/AVG devuelven None.
        """
        table = ast["table"]
        t = time.perf_counter()
        columns = self._get_columns(table)
        col_map = {c.name: c for c in columns}
        aggs: list[dict] = []
        for item in ast["columns"]:
            if item["agg"] is None:
                raise SemanticError(
                    "no se pueden mezclar columnas con agregados")
            fn = item["agg"]
            if fn == "count":
                aggs.append({"fn": fn, "column": None, "pos": None})
                continue
            col = col_map.get(item["column"])
            if col is None:
                raise SemanticError(
                    f"la columna '{item['column']}' no existe en '{table}'")
            if col.type not in self._AGG_TYPES[fn]:
                raise SemanticError(
                    f"{fn.upper()} no soporta el tipo {col.type_str()}")
            aggs.append({"fn": fn, "column": col.name,
                         "pos": [c.name for c in columns].index(col.name)})
        where = ast["where"]
        if where is not None:
            self._check_where_columns(table, where)
        detail = ", ".join(
            f"{a['fn']}(*)" if a["fn"] == "count"
            else f"{a['fn']}({a['column']})" for a in aggs)
        plan.add("Semantic Check",
                 f"agregados válidos sobre '{table}': {detail}", t)

        # Mismo método de acceso que un SELECT normal: índice si existe.
        rids: list[RID] | None = None
        ordered_knn: list[tuple[RID, float]] | None = None
        residual: dict | None = None
        if where is not None:
            rids, ordered_knn, residual = self._plan_access(table, where, plan)

        t = time.perf_counter()
        heap = self._open_storage(table)
        # COUNT(*) sin WHERE no necesita deserializar: cuenta registros
        # crudos del scan (con WHERE el _match ya exige deserializar).
        needs_values = any(a["fn"] != "count" for a in aggs)
        acc = [{"total": 0, "n": 0, "best": None, "seen": False}
               for _ in aggs]
        count = 0
        fetched = 0
        if rids is not None:  # acceso por índice (incl. KNN por R-Tree)
            fetched = len(rids)
            if residual is None and not needs_values:
                count = len(rids)
            else:
                for rid in rids:
                    row = deserialize_row(columns, self._read_row(heap, rid))
                    if residual is not None and not self._match(
                            columns, residual, row, table):
                        continue
                    count += 1
                    if needs_values:
                        self._accumulate(aggs, acc, row)
        else:  # escaneo secuencial
            for rid, raw in self._scan_rows(heap):
                fetched += 1
                if where is not None:
                    row = deserialize_row(columns, raw)
                    if not self._match(columns, where, row, table):
                        continue
                elif needs_values:
                    row = deserialize_row(columns, raw)
                else:
                    row = None
                count += 1
                if row is not None:
                    self._accumulate(aggs, acc, row)
        heap.close()
        plan.add("Fetch Rows", f"{fetched} registros leídos del heap", t)

        t = time.perf_counter()
        out_cols: list[str] = []
        out_row: list = []
        for a, st in zip(aggs, acc):
            if a["fn"] == "count":
                out_cols.append("count")
                out_row.append(count)
            else:
                out_cols.append(f"{a['fn']}_{a['column']}")
                out_row.append(self._agg_result(a, st))
        plan.add("Aggregate", detail, t)
        return {"kind": "select", "columns": out_cols, "rows": [out_row],
                "rowcount": 1, "message": "OK", "spatial": None,
                "truncated": False}

    @staticmethod
    def _accumulate(aggs: list[dict], acc: list[dict], row: list) -> None:
        """Actualiza los acumuladores con una fila (un solo pase)."""
        for a, st in zip(aggs, acc):
            if a["fn"] == "count":
                continue
            v = row[a["pos"]]
            if a["fn"] == "min":
                if not st["seen"] or v < st["best"]:
                    st["best"] = v
            elif a["fn"] == "max":
                if not st["seen"] or v > st["best"]:
                    st["best"] = v
            else:  # sum / avg
                st["total"] += v
                st["n"] += 1
            st["seen"] = True

    @staticmethod
    def _agg_result(a: dict, st: dict):
        if a["fn"] == "min" or a["fn"] == "max":
            return st["best"] if st["seen"] else None
        if not st["seen"]:  # cero filas: sum/avg son NULL
            return None
        if a["fn"] == "sum":
            return st["total"]
        return st["total"] / st["n"]  # avg: siempre float

    def _split_and(self, table: str, where: dict) -> tuple[dict, dict | None]:
        """De una conjunción AND elige la condición con mejor método de
        acceso como driver y devuelve (driver, filtro residual o None)."""
        conds = where["conditions"]
        driver = max(conds, key=lambda c: self._access_score(table, c))
        rest = [c for c in conds if c is not driver]
        if not rest:
            residual = None
        elif len(rest) == 1:
            residual = rest[0]
        else:
            residual = {"kind": "and", "conditions": rest}
        return driver, residual

    def _access_score(self, table: str, cond: dict) -> int:
        """Prioridad de una condición como método de acceso:
        4 = KNN o MATCH con ranking (siempre gobierna: es top-k ordenado,
            no un predicado; también sin índice: fuerza su rol de driver),
        3 = igualdad con índice (o búsqueda binaria en PK sequential),
        2 = rango/espacial con índice (o rango en PK sequential),
        1 = radio espacial o MATCH booleano sin índice (fuerza bruta),
        0 = sin acceso directo (solo sirve de filtro)."""
        col = self._get_column(table, cond["column"])
        kind = cond["kind"]
        if kind == "match":
            if (col.type in (TYPE_TEXT, TYPE_VARCHAR)
                    and self.catalog.index_on(table, col.name, {"INVERTED"})):
                return 4
            return 4 if cond.get("ranked") else 1
        pk = self.catalog.primary_key(table)
        if (pk is not None and col.name == pk.name
                and kind in ("compare", "between")
                and self._is_sequential(table)):
            if kind == "compare" and cond["op"] == "=":
                return 3
            return 2
        if kind == "compare":
            if (cond["op"] == "=" and col.type != TYPE_POINT
                    and self.catalog.index_on(table, col.name,
                                              {"BTREE", "HASH"})):
                return 3
            if (cond["op"] in ("<", "<=", ">", ">=") and col.type != TYPE_POINT
                    and self.catalog.index_on(table, col.name, {"BTREE"})):
                return 2
            return 0
        if kind == "between":
            if (col.type != TYPE_POINT
                    and self.catalog.index_on(table, col.name, {"BTREE"})):
                return 2
            return 0
        # KNN no es un predicado booleano (es top-k ordenado): si aparece
        # en una conjunción siempre gobierna el acceso y el resto del AND
        # queda como filtro residual sobre sus resultados.
        if kind == "knn":
            return 4
        if kind == "radius":
            if (col.type == TYPE_POINT
                    and self.catalog.index_on(table, col.name, {"RTREE"})):
                return 2
            return 1
        return 0

    def _check_where_columns(self, table: str, where: dict) -> None:
        """Valida que todas las columnas del WHERE existan en la tabla."""
        if where["kind"] == "and":
            for cond in where["conditions"]:
                self._get_column(table, cond["column"])
        else:
            self._get_column(table, where["column"])

    @staticmethod
    def _knn_condition(where: dict | None) -> dict | None:
        """La condición KNN del WHERE (directa o dentro de un AND), si hay."""
        if where is None:
            return None
        if where["kind"] == "knn":
            return where
        if where["kind"] == "and":
            return next((c for c in where["conditions"]
                         if c["kind"] == "knn"), None)
        return None

    @staticmethod
    def _match_condition(where: dict | None) -> dict | None:
        """La condición MATCH del WHERE (directa o dentro de un AND)."""
        if where is None:
            return None
        if where["kind"] == "match":
            return where
        if where["kind"] == "and":
            return next((c for c in where["conditions"]
                         if c["kind"] == "match"), None)
        return None

    # ------------------------------------------------------------------
    # Búsqueda textual: ranking TF-IDF y evaluación booleana por fila
    # ------------------------------------------------------------------
    @staticmethod
    def _ranked_query_terms(q_ast: dict, pre: TextPreprocessor) -> list[str]:
        """Términos de la consulta para el ranking coseno.

        El ranking es un modelo de bolsa de términos: se admiten términos
        sueltos o conjunciones (AND explícito o implícito), pero no OR ni
        NOT, que solo tienen sentido en la recuperación booleana.
        """
        terms: list[str] = []

        def collect(node: dict) -> None:
            if "term" in node:
                terms.append(node["term"])
            elif "and" in node:
                for child in node["and"]:
                    collect(child)
            else:
                raise SemanticError(
                    "COSINE_SCORE() usa un ranking por bolsa de términos: "
                    "la consulta MATCH no admite OR ni NOT "
                    "(esos operadores son para la búsqueda booleana sin "
                    "COSINE_SCORE)")

        collect(q_ast)
        return pre.terms(" ".join(terms))

    @staticmethod
    def _eval_boolean(q_ast: dict, doc_terms: set[str],
                      pre: TextPreprocessor) -> bool:
        """Evalúa el AST booleano del MATCH contra los términos de una fila
        (línea base sin índice: Full Text Scan)."""
        if "term" in q_ast:
            terms = pre.terms(q_ast["term"])
            return bool(terms) and all(t in doc_terms for t in terms)
        if "and" in q_ast:
            return all(Engine._eval_boolean(c, doc_terms, pre)
                       for c in q_ast["and"])
        if "or" in q_ast:
            return any(Engine._eval_boolean(c, doc_terms, pre)
                       for c in q_ast["or"])
        if "not" in q_ast:
            return not Engine._eval_boolean(q_ast["not"], doc_terms, pre)
        return False

    def _fulltext_rank(self, table: str, columns: list[Column],
                       rows: list[tuple[RID, list]], match_cond: dict,
                       k: int) -> list[tuple[RID, list, float]]:
        """Ranking TF-IDF por fuerza bruta (sin índice INVERTED).

        Dos pasadas sobre las filas escaneadas: la primera calcula el df
        de cada término (de ahí el idf) y la segunda puntúa cada fila con
        similitud de coseno. Es la línea base del Experimento 6.
        Devuelve ``[(rid, row, score)]`` ordenado desc y truncado a ``k``.
        """
        col = self._get_column(table, match_cond["column"])
        pos = [c.name for c in columns].index(col.name)
        pre = self._preprocessor(self._inverted_params(table, col.name))
        q_ast = parse_match_query(match_cond["query"])
        query_terms = self._ranked_query_terms(q_ast, pre)

        counts: list[collections.Counter] = []
        df: dict[str, int] = {}
        for _rid, row in rows:
            c = collections.Counter(pre.terms(str(row[pos])))
            counts.append(c)
            for term in c:
                df[term] = df.get(term, 0) + 1
        n = len(rows)
        if n == 0 or not query_terms:
            return []
        idf = {t: math.log10(n / d) for t, d in df.items()}

        q_counts = collections.Counter(query_terms)
        q_weights = {t: (1.0 + math.log10(c)) * idf[t]
                     for t, c in q_counts.items() if t in idf}
        q_norm = math.sqrt(sum(w * w for w in q_weights.values()))
        if q_norm == 0.0:
            return []

        scored: list[tuple[RID, list, float]] = []
        for (rid, row), c in zip(rows, counts):
            dot = 0.0
            norm_sq = 0.0
            for t, tf in c.items():
                w = (1.0 + math.log10(tf)) * idf[t]
                norm_sq += w * w
                if t in q_weights:
                    dot += q_weights[t] * w
            if dot > 0.0 and norm_sq > 0.0:
                scored.append((rid, row, dot / (math.sqrt(norm_sq) * q_norm)))
        scored.sort(key=lambda t: t[2], reverse=True)
        return scored[:k]

    def _plan_access(self, table: str, where: dict,
                     plan: _Plan,
                     limit_hint: int | None = None) -> tuple[
                         list[RID] | None,
                         list[tuple[RID, float]] | None,
                         dict | None]:
        """Elige el método de acceso y devuelve (RIDs, KNN/MATCH ordenado,
        filtro residual). RIDs None = seq scan; con una conjunción AND la
        mejor condición gobierna el acceso y el resto queda como filtro
        residual sobre las tuplas recuperadas. ``limit_hint`` es el K del
        Top-K textual (offset+limit o el tope de seguridad)."""
        kind = where["kind"]
        if kind == "and":
            driver, residual = self._split_and(table, where)
            rids, ordered_knn, _ = self._plan_access(
                table, driver, plan, limit_hint=limit_hint)
            t = time.perf_counter()
            n = len(where["conditions"]) - 1
            plan.add("Residual Filter",
                     f"AND: {n} condición(es) restante(s) filtran las "
                     f"tuplas recuperadas", t)
            return rids, ordered_knn, residual

        col = self._get_column(table, where["column"])

        pk = self.catalog.primary_key(table)
        if (pk is not None and col.name == pk.name
                and kind in ("compare", "between")
                and self._is_sequential(table)):
            return self._plan_access_sequential(table, pk, where, plan)

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
                    return rids, None, None
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
                    return rids, None, None
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin índice usable para {table}.{col.name} {op}", t)
            return None, None, None

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
                return rids, None, None
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin índice BTREE para {table}.{col.name}", t)
            return None, None, None

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
                    return rids, None, None
                results = idx.knn(where["center"], where["k"])
                idx.close()
                plan.add("R-Tree KNN Search",
                         f"USING RTREE ON {table}.{col.name} "
                         f"centro={where['center']} k={where['k']}", t)
                return [rid for rid, _ in results], results, None
            # Respaldo sin índice: fuerza bruta
            t = time.perf_counter()
            plan.add("Sequential Scan",
                     f"sin RTREE en {table}.{col.name}: "
                     f"cómputo espacial por fuerza bruta", t)
            return None, None, None

        if kind == "match":
            if col.type not in (TYPE_TEXT, TYPE_VARCHAR):
                raise SemanticError(
                    f"MATCH requiere una columna TEXT o VARCHAR, "
                    f"'{col.name}' es {col.type_str()}")
            try:
                q_ast = parse_match_query(where["query"])
            except MatchQueryError as exc:
                raise SemanticError(f"consulta MATCH inválida: {exc}") from exc
            ranked = bool(where.get("ranked"))
            meta = self.catalog.index_on(table, col.name, {"INVERTED"})
            if meta is None:
                t = time.perf_counter()
                plan.add("Full Text Scan",
                         f"sin INVERTED en {table}.{col.name}: "
                         f"{'ranking TF-IDF' if ranked else 'filtro booleano'}"
                         f" por fuerza bruta", t)
                return None, None, None
            pre = self._preprocessor(meta.get("params") or {})
            t = time.perf_counter()
            idx = self._open_inverted(table, col)
            if ranked:
                terms = self._ranked_query_terms(q_ast, pre)
                k = limit_hint or SELECT_ROW_CAP
                scored = idx.cosine_topk(terms, k)
                pairs = [( (idx.docs[d][0], idx.docs[d][1]), s)
                         for d, s in scored]
                idx.close()
                plan.add("Inverted Index TopK Scan",
                         f"USING INVERTED ON {table}.{col.name} "
                         f"MATCH {where['query']!r} -> "
                         f"{len(pairs)} candidatos (k={k})", t)
                return [rid for rid, _s in pairs], pairs, None
            doc_ids = idx.search_boolean(q_ast, pre)
            rids = [(idx.docs[d][0], idx.docs[d][1]) for d in doc_ids]
            idx.close()
            plan.add("Inverted Index Boolean Scan",
                     f"USING INVERTED ON {table}.{col.name} "
                     f"MATCH {where['query']!r} -> {len(rids)} RIDs", t)
            return rids, None, None

        raise ExecutionError(f"condición no soportada: {kind}")

    def _plan_access_sequential(self, table: str, pk: Column, where: dict,
                                plan: _Plan) -> tuple[list[RID], None, None]:
        """Acceso por la PK de una tabla SEQUENTIAL: búsqueda binaria en el
        área principal + recorrido de la cadena del overflow."""
        storage = self._open_storage(table)
        kind = where["kind"]
        t = time.perf_counter()
        if kind == "compare":
            op = where["op"]
            value = coerce_value(where["value"], pk)
            if op == "=":
                found = storage.search(value)
                rids = [found[0]] if found is not None else []
                detail = (f"{table}.{pk.name} = {value!r} "
                          f"-> {len(rids)} RIDs")
            else:
                if op == "<":
                    res = storage.range_search(hi=value, hi_inc=False)
                elif op == "<=":
                    res = storage.range_search(hi=value)
                elif op == ">":
                    res = storage.range_search(lo=value, lo_inc=False)
                else:  # ">="
                    res = storage.range_search(lo=value)
                rids = [rid for rid, _k, _p in res]
                detail = (f"{table}.{pk.name} {op} {value!r} "
                          f"-> {len(rids)} RIDs")
        else:  # between
            low = coerce_value(where["low"], pk)
            high = coerce_value(where["high"], pk)
            res = storage.range_search(low, high)
            rids = [rid for rid, _k, _p in res]
            detail = (f"{table}.{pk.name} BETWEEN {low!r} AND {high!r} "
                      f"-> {len(rids)} RIDs")
        storage.close()
        plan.add("Binary Search (Sequential)", detail, t)
        return rids, None, None

    # ------------------------------------------------------------------
    # Filtro en escaneo secuencial
    # ------------------------------------------------------------------
    def _match(self, columns: list[Column], where: dict, row: list,
               table: str | None = None) -> bool:
        kind = where["kind"]
        if kind == "and":
            return all(self._match(columns, c, row, table)
                       for c in where["conditions"])
        col = next(c for c in columns if c.name == where["column"])
        pos = [c.name for c in columns].index(col.name)
        value = row[pos]
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
        if kind == "match":
            if where.get("ranked"):
                return True  # el ranking se aplica tras el escaneo
            # MATCH booleano sin índice INVERTED: evaluación por fila
            if table is None:
                raise ExecutionError(
                    "MATCH booleano requiere el contexto de la tabla")
            pre = self._preprocessor(
                self._inverted_params(table, where["column"]))
            q_ast = parse_match_query(where["query"])
            doc_terms = set(pre.terms(str(value)))
            return self._eval_boolean(q_ast, doc_terms, pre)
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
        rids, _, _ = self._plan_access(table, where, plan)
        if rids is None:
            t = time.perf_counter()
            heap = self._open_storage(table)
            rids = [rid for rid, raw in self._scan_rows(heap)
                    if self._match(columns, where,
                                   deserialize_row(columns, raw), table)]
            heap.close()
            plan.add("Filter", f"{len(rids)} registros coinciden", t)

        t = time.perf_counter()
        heap = self._open_storage(table)
        victims = [(rid, deserialize_row(columns, self._read_row(heap, rid)))
                   for rid in rids]
        if isinstance(heap, SequentialFile):
            # El SequentialFile elimina por clave (la PK de la fila).
            pk_col = self.catalog.primary_key(table)
            pk_pos = [c.name for c in columns].index(pk_col.name)
            for _rid, row in victims:
                heap.delete(row[pk_pos])
        else:
            for rid, _ in victims:
                heap.delete(rid)
        heap.close()
        plan.add("Heap Delete", f"{len(victims)} registros eliminados", t)

        for meta in self.catalog.indexes(table):
            t = time.perf_counter()
            icol = self._get_column(table, meta["column"])
            pos = [c.name for c in columns].index(icol.name)
            if meta["type"] == "INVERTED":
                idx = self._open_inverted(table, icol)
                for rid, _row in victims:
                    idx.delete_document(rid)
            elif meta["type"] == "IVF":
                idx = self._open_ivf(table, icol)
                for rid, row in victims:
                    idx.delete(rid, tuple(row[pos]))
            else:
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
            organization = self.catalog.organization(name)
            heap = self._open_storage(name)
            rowcount = heap.row_count
            heap.close()
            if organization == "sequential":
                files = [self._file_info(p) for p in self._seq_paths(name)
                         if os.path.isfile(p)]
            else:
                files = [self._file_info(self._heap_path(name))]
            for meta in self.catalog.indexes(name):
                for p in self._index_paths(name, meta["column"], meta["type"]):
                    files.append(self._file_info(p))
            out.append({
                "name": name,
                "organization": organization,
                "columns": [
                    {"name": c.name, "type": c.type_str(),
                     "primary_key": c.primary_key, "auto": c.auto}
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

    def reorganize(self, table: str, fill_factor: float = 0.75) -> dict:
        """Reorganiza el archivo SEQUENTIAL de ``table`` y devuelve stats.

        Uso del endpoint ``POST /api/tables/{name}/reorganize``; la
        validación de existencia/organización la hace la ruta.
        """
        storage = self._open_storage(table)
        try:
            return storage.reorganize(fill_factor)
        finally:
            storage.close()

    @staticmethod
    def _file_info(path: str) -> dict:
        size = os.path.getsize(path)
        pages = (size + 4095) // 4096
        return {"path": os.path.abspath(path), "size_bytes": size,
                "pages": pages}
