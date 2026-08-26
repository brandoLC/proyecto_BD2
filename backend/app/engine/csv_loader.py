"""Carga de archivos CSV: lectura, inferencia de esquema y casteo de valores.

Convenciones del CSV soportado:

- La primera fila es la cabecera (nombres de columna) y el delimitador se
  detecta con ``csv.Sniffer`` (coma como respaldo).
- Los valores vacíos se tratan como null-ish: no descalifican un tipo en
  la inferencia y se rechazan al castear a un tipo escalar.
- Los puntos se escriben ``(x, y)`` con espacios opcionales, p. ej.
  ``( -12.0464, -77.0428 )``.

La inferencia muestrea hasta ``MAX_SAMPLE_ROWS`` filas: si todos los
valores no vacíos son enteros el tipo es ``INT``; si todos son numéricos,
``FLOAT``; si todos son ``true/false``, ``BOOL``; si todos son puntos,
``POINT``; en otro caso ``VARCHAR(n)`` con un 20 % de holgura redondeado
a múltiplos de 10 (mínimo 20), o ``TEXT`` si supera 255. Se sugiere
PRIMARY KEY cuando la primera columna es ``INT`` con valores únicos.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re

from ..storage.record import (
    Column,
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_POINT,
    TYPE_TEXT,
    TYPE_VARCHAR,
)
from .parser import KEYWORDS

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
BOOL_RE = re.compile(r"^(?:true|false)$", re.IGNORECASE)
POINT_RE = re.compile(
    r"^\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)$"
)

MAX_SAMPLE_ROWS = 200
MAX_ERRORS = 50


class CSVError(Exception):
    """CSV mal formado: vacío, sin cabecera o cabecera inválida."""


# ----------------------------------------------------------------------
# Lectura
# ----------------------------------------------------------------------
def decode_csv_bytes(data: bytes) -> str:
    """Decodifica el CSV (UTF-8 con BOM opcional; respaldo Latin-1)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_csv(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Parsea el CSV y devuelve ``(cabecera, [(línea, fila), ...])``.

    La cabecera es la línea 1; cada fila de datos guarda su número de
    línea físico en el archivo. Las líneas en blanco se omiten.
    """
    if not text.strip():
        raise CSVError("el archivo CSV está vacío")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue  # línea en blanco
        if header is None:
            header = [cell.strip() for cell in row]
            if any(not h for h in header):
                raise CSVError("la cabecera tiene nombres de columna vacíos")
            lowered = [h.lower() for h in header]
            if len(set(lowered)) != len(lowered):
                raise CSVError(
                    "la cabecera tiene columnas con nombre duplicado")
        else:
            rows.append((reader.line_num, row))
    if header is None:
        raise CSVError("el CSV no tiene cabecera")
    return header, rows


# ----------------------------------------------------------------------
# Inferencia de esquema
# ----------------------------------------------------------------------
def sanitize_identifier(name: str) -> str:
    """Convierte un texto en un identificador SQL válido de MiniDB."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    if not s or s[0].isdigit() or s.upper() in KEYWORDS:
        s = "t_" + s
    return s


def _varchar_size(max_len: int) -> tuple[str, int | None]:
    """VARCHAR(n) con 20% de margen redondeado a decenas; TEXT si excede 255."""
    n = math.ceil(max_len * 1.2)
    n = max(20, math.ceil(n / 10) * 10)
    if n > 255:
        return TYPE_TEXT, None
    return TYPE_VARCHAR, n


def _infer_type(values: list[str]) -> tuple[str, int | None]:
    """Tipo de columna a partir de sus valores no vacíos del muestreo."""
    if not values:
        return TYPE_TEXT, None
    if all(INT_RE.match(v) for v in values):
        return TYPE_INT, None
    if all(FLOAT_RE.match(v) for v in values):
        return TYPE_FLOAT, None
    if all(BOOL_RE.match(v) for v in values):
        return TYPE_BOOL, None
    if all(POINT_RE.match(v) for v in values):
        return TYPE_POINT, None
    return _varchar_size(max(len(v) for v in values))


def infer_columns(header: list[str], rows: list[list[str]],
                  sample_limit: int = MAX_SAMPLE_ROWS) -> list[Column]:
    """Infiere las columnas muestreando hasta ``sample_limit`` filas.

    La detección de tipo (INT/FLOAT/BOOL/POINT) usa el muestreo, pero el
    tamaño de los VARCHAR se calcula con el valor más largo de TODO el
    archivo: un muestreo corto subestima y la carga rechazaría filas.
    """
    sample = [r for r in rows[:sample_limit] if len(r) == len(header)]
    used: set[str] = set()
    columns: list[Column] = []
    for j, name in enumerate(header):
        values = [r[j].strip() for r in sample if r[j].strip() != ""]
        ident = sanitize_identifier(name)
        if ident in used:
            k = 2
            while f"{ident}_{k}" in used:
                k += 1
            ident = f"{ident}_{k}"
        used.add(ident)
        ctype, size = _infer_type(values)
        if ctype == TYPE_VARCHAR:
            full_max = max((len(r[j].strip()) for r in rows
                            if len(r) == len(header)), default=0)
            ctype, size = _varchar_size(full_max)
        columns.append(Column(ident, ctype, size))
    # PRIMARY KEY sugerida: primera columna INT con valores únicos
    if columns and columns[0].type == TYPE_INT:
        first = [r[0].strip() for r in sample if r[0].strip() != ""]
        if first and len(set(first)) == len(first):
            columns[0].primary_key = True
    return columns


def suggested_create_sql(table: str, columns: list[Column]) -> str:
    """Sentencia CREATE TABLE sugerida para el esquema inferido."""
    defs = ", ".join(
        f"{c.name} {c.type_str()}" + (" PRIMARY KEY" if c.primary_key else "")
        for c in columns
    )
    return f"CREATE TABLE {table} ({defs});"


# ----------------------------------------------------------------------
# Mapeo y casteo para la carga
# ----------------------------------------------------------------------
def map_columns(header: list[str],
                table_columns: list[Column]) -> tuple[list[int], list[str]]:
    """Mapea la cabecera del CSV a las columnas de la tabla por nombre.

    Devuelve ``(posiciones, ignoradas)``: la posición en la fila CSV de
    cada columna de la tabla (en el orden del esquema) y los nombres de
    las columnas del CSV que la tabla no tiene. Lanza ``ValueError`` si
    falta alguna columna requerida.
    """
    by_name = {h.strip().lower(): i for i, h in enumerate(header)}
    positions: list[int] = []
    used: set[int] = set()
    for col in table_columns:
        i = by_name.get(col.name.lower())
        if i is None:
            raise ValueError(
                f"falta la columna requerida '{col.name}' en el CSV")
        positions.append(i)
        used.add(i)
    ignored = [h for i, h in enumerate(header) if i not in used]
    return positions, ignored


def reorder_row(raw: list[str], positions: list[int]) -> list[str]:
    """Reordena una fila cruda según las posiciones del mapeo."""
    return [raw[i] if i < len(raw) else "" for i in positions]


def cast_csv_value(raw: str, col: Column):
    """Castea un valor crudo del CSV al tipo de la columna.

    Lanza ``ValueError`` con un mensaje ``col: 'valor' no es TIPO``.
    """
    v = raw.strip()
    if col.type == TYPE_VARCHAR:
        if len(v.encode("utf-8")) > (col.size or 0):
            raise ValueError(
                f"{col.name}: {raw!r} excede {col.type_str()}")
        return v
    if col.type == TYPE_TEXT:
        return v
    ok = False
    try:
        if col.type == TYPE_INT:
            ok = bool(INT_RE.match(v))
            result = int(v) if ok else None
        elif col.type == TYPE_FLOAT:
            ok = bool(FLOAT_RE.match(v))
            result = float(v) if ok else None
        elif col.type == TYPE_BOOL:
            low = v.lower()
            ok = low in ("true", "false", "1", "0")
            result = low in ("true", "1")
        elif col.type == TYPE_POINT:
            m = POINT_RE.match(v)
            ok = m is not None
            result = (float(m.group(1)), float(m.group(2))) if ok else None
        else:
            raise ValueError(f"tipo desconocido: {col.type}")
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{col.name}: {raw!r} no es {col.type_str()}")
    return result


def resolve_dataset_path(datasets_dir: str, filename: str) -> str:
    """Resuelve ``filename`` dentro de ``datasets_dir`` rechazando
    rutas absolutas y traversal (``..``). Devuelve la ruta absoluta."""
    if os.path.isabs(filename):
        raise ValueError(f"ruta absoluta no permitida: '{filename}'")
    base = os.path.realpath(datasets_dir)
    path = os.path.realpath(os.path.join(base, filename))
    if path != base and os.path.commonpath([base, path]) != base:
        raise ValueError(f"ruta fuera del directorio de datasets: "
                         f"'{filename}'")
    return path
