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
``POINT``; en otro caso ``VARCHAR(n)``. Los tipos numéricos se verifican
luego contra TODO el archivo y degradan a ``VARCHAR`` si aparece un valor
que no casa (p. ej. códigos postales ZIP+4 ``"39452-6632"``); los valores
con ceros a la izquierda (``"05301"``) nunca se infieren como números.
El tamaño de ``VARCHAR(n)`` usa la longitud máxima de todo el archivo
con un 20 % de holgura redondeado a múltiplos de 10 (mínimo 20), o
``TEXT`` si supera 255. Se sugiere PRIMARY KEY cuando la primera columna
es ``INT`` con valores únicos.

Además se detectan pares de columnas latitud/longitud (aliases
``latitude/lat/latitud`` y ``longitude/lng/lon/long/longitud``, con
valores en rango geográfico) para derivar una columna ``POINT``
``(lat, lng)`` aprovechable por los índices R-Tree.
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
LEADING_ZERO_RE = re.compile(r"^-?0\d")  # "05301" es identificador, no número

MAX_SAMPLE_ROWS = 200
MAX_ERRORS = 50


def _matches(regex: re.Pattern, v: str) -> bool:
    """``regex`` casa con ``v`` y sin ceros a la izquierda (son códigos)."""
    return bool(regex.match(v)) and not LEADING_ZERO_RE.match(v)


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
    if all(_matches(INT_RE, v) for v in values):
        return TYPE_INT, None
    if all(_matches(FLOAT_RE, v) for v in values):
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
        if ctype in (TYPE_INT, TYPE_FLOAT):
            # Verificación contra TODO el archivo (estilo pandas): si hay
            # valores fuera del muestreo que no casan (p. ej. códigos
            # postales ZIP+4 "39452-6632"), la columna degrada a VARCHAR
            # en vez de rechazar filas legítimas al cargar.
            regex = INT_RE if ctype == TYPE_INT else FLOAT_RE
            full = [r[j].strip() for r in rows
                    if len(r) == len(header) and r[j].strip() != ""]
            if not all(_matches(regex, v) for v in full):
                ctype, size = _varchar_size(
                    max((len(v) for v in full), default=0))
        elif ctype == TYPE_VARCHAR:
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
# Detección de pares latitud/longitud -> columna POINT derivada
# ----------------------------------------------------------------------
LAT_ALIASES = ("latitude", "lat", "latitud")
LNG_ALIASES = ("longitude", "lng", "lon", "long", "longitud")


def _dedup_name(base: str, used: set[str]) -> str:
    """``base`` o ``base_2``, ``base_3``, ... según lo ya usado."""
    name = base
    k = 2
    while name in used:
        name = f"{base}_{k}"
        k += 1
    return name


def _in_geo_range(rows: list[list[str]], j: int,
                  lo: float, hi: float) -> bool:
    """Todos los valores no vacíos de la columna ``j`` caen en [lo, hi]."""
    vals = [r[j].strip() for r in rows if r[j].strip() != ""]
    if not vals:
        return False
    try:
        return all(lo <= float(v) <= hi for v in vals)
    except ValueError:
        return False


def detect_point_pair(header: list[str], columns: list[Column],
                      rows: list[list[str]]) -> dict | None:
    """Detecta un par de columnas lat/lng derivable a una columna POINT.

    Busca por nombre saneado (los alias son case-insensitive por
    construcción) sin importar el orden de las columnas; ambas deben ser
    FLOAT/INT y sus valores de TODO el archivo deben caer en rango
    geográfico (latitud en [-90, 90], longitud en [-180, 180]) para
    descartar falsos positivos. Si hay varios pares plausibles se usa el
    primero. Devuelve ``{"column", "lat_col", "lng_col"}`` o ``None``; el
    nombre de la columna derivada es ``location`` (o ``location_2``... si
    ya existe).
    """
    full = [r for r in rows if len(r) == len(header)]
    lats = [j for j, c in enumerate(columns)
            if c.name in LAT_ALIASES and c.type in (TYPE_FLOAT, TYPE_INT)]
    lngs = [j for j, c in enumerate(columns)
            if c.name in LNG_ALIASES and c.type in (TYPE_FLOAT, TYPE_INT)]
    for j in lats:
        for k in lngs:
            if j == k:
                continue
            if (_in_geo_range(full, j, -90.0, 90.0)
                    and _in_geo_range(full, k, -180.0, 180.0)):
                return {
                    "column": _dedup_name("location",
                                          {c.name for c in columns}),
                    "lat_col": columns[j].name,
                    "lng_col": columns[k].name,
                }
    return None


def column_position(header: list[str], name: str) -> int | None:
    """Posición de ``name`` en la cabecera (strip + case-insensitive)."""
    target = name.strip().lower()
    for i, h in enumerate(header):
        if h.strip().lower() == target:
            return i
    return None


def derive_point_value(lat_raw: str, lng_raw: str,
                       col_name: str, line_no: int) -> str:
    """Construye el valor crudo ``"(lat, lng)"`` de la columna derivada.

    Convención del motor: un punto es ``(x, y)`` con x = latitud e
    y = longitud. Lanza ``ValueError`` si algún valor no es numérico.
    """
    try:
        lat = float(lat_raw.strip())
        lng = float(lng_raw.strip())
    except (ValueError, AttributeError):
        raise ValueError(
            f"{col_name}: lat/lng inválidos en línea {line_no}") from None
    return f"({lat}, {lng})"


# ----------------------------------------------------------------------
# Mapeo y casteo para la carga
# ----------------------------------------------------------------------
def map_columns(header: list[str],
                table_columns: list[Column]) -> tuple[list[int], list[str]]:
    """Mapea la cabecera del CSV a las columnas de la tabla por nombre.

    Las cabeceras se normalizan con ``sanitize_identifier`` (el mismo
    criterio con el que la inferencia genera los nombres de columna), así
    un CSV con tildes o espacios ("Código Modular") coincide con la
    columna ``c_digo_modular`` de la tabla.

    Devuelve ``(posiciones, ignoradas)``: la posición en la fila CSV de
    cada columna de la tabla (en el orden del esquema) y los nombres de
    las columnas del CSV que la tabla no tiene. Lanza ``ValueError`` si
    falta alguna columna requerida.
    """
    by_name: dict[str, int] = {}
    for i, h in enumerate(header):
        name = sanitize_identifier(h)
        if name in by_name:
            raise ValueError(
                f"dos columnas del CSV se traducen al mismo nombre "
                f"'{name}' ('{header[by_name[name]]}' y '{h}')")
        by_name[name] = i
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
