"""Serialización de registros con ``struct`` según el esquema de la tabla.

Mapeo de tipos a códigos de formato:

- ``INT``        -> ``i`` (4 bytes)
- ``FLOAT``      -> ``d`` (8 bytes)
- ``BOOL``       -> ``?`` (1 byte)
- ``VARCHAR(n)`` -> ``{n}s`` (n bytes, padding con NUL)
- ``POINT``      -> ``dd`` (dos floats x, y)
- ``TEXT``       -> prefijo de longitud ``H`` seguido de los bytes

Si la tabla no tiene columnas ``TEXT`` el registro es de longitud fija.
También incluye la codificación de claves de índice de longitud fija
(``encode_key``/``decode_key``) usada por B+ Tree, Hash y R-Tree.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TYPE_INT = "INT"
TYPE_FLOAT = "FLOAT"
TYPE_BOOL = "BOOL"
TYPE_VARCHAR = "VARCHAR"
TYPE_TEXT = "TEXT"
TYPE_POINT = "POINT"

SCALAR_TYPES = {TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_VARCHAR, TYPE_TEXT}

# Longitud máxima de una clave TEXT cuando se indexa (se trunca).
MAX_TEXT_KEY = 64


@dataclass
class Column:
    """Definición de columna en el esquema de una tabla."""

    name: str
    type: str
    size: int | None = None  # solo para VARCHAR(n)
    primary_key: bool = False

    def type_str(self) -> str:
        if self.type == TYPE_VARCHAR:
            return f"VARCHAR({self.size})"
        return self.type

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "size": self.size,
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Column":
        return cls(
            name=d["name"],
            type=d["type"],
            size=d.get("size"),
            primary_key=bool(d.get("primary_key", False)),
        )


class SerializationError(Exception):
    """Valor incompatible con el tipo de la columna."""


# ----------------------------------------------------------------------
# Serialización de filas completas
# ----------------------------------------------------------------------
def coerce_value(value, col: Column):
    """Valida/coerce un valor Python al tipo de la columna."""
    try:
        if col.type == TYPE_INT:
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if col.type == TYPE_FLOAT:
            return float(value)
        if col.type == TYPE_BOOL:
            if isinstance(value, bool):
                return value
            raise ValueError
        if col.type == TYPE_VARCHAR:
            s = str(value)
            if len(s.encode("utf-8")) > (col.size or 0):
                raise SerializationError(
                    f"valor excede VARCHAR({col.size}) en columna '{col.name}'"
                )
            return s
        if col.type == TYPE_TEXT:
            return str(value)
        if col.type == TYPE_POINT:
            x, y = value
            return (float(x), float(y))
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            f"valor {value!r} incompatible con {col.type} (columna '{col.name}')"
        ) from exc
    raise SerializationError(f"tipo desconocido: {col.type}")


def serialize_row(columns: list[Column], values: list) -> bytes:
    """Empaqueta una fila completa en bytes usando ``struct``."""
    if len(values) != len(columns):
        raise SerializationError(
            f"se esperaban {len(columns)} valores, llegaron {len(values)}"
        )
    out = bytearray()
    for col, val in zip(columns, values):
        v = coerce_value(val, col)
        if col.type == TYPE_INT:
            out += struct.pack("<i", v)
        elif col.type == TYPE_FLOAT:
            out += struct.pack("<d", v)
        elif col.type == TYPE_BOOL:
            out += struct.pack("<?", v)
        elif col.type == TYPE_VARCHAR:
            out += struct.pack(f"<{col.size}s", v.encode("utf-8"))
        elif col.type == TYPE_POINT:
            out += struct.pack("<dd", v[0], v[1])
        elif col.type == TYPE_TEXT:
            data = v.encode("utf-8")
            out += struct.pack("<H", len(data)) + data
    return bytes(out)


def deserialize_row(columns: list[Column], data: bytes) -> list:
    """Desempaqueta bytes a la lista de valores Python de la fila."""
    values: list = []
    pos = 0
    for col in columns:
        if col.type == TYPE_INT:
            (v,) = struct.unpack_from("<i", data, pos)
            pos += 4
        elif col.type == TYPE_FLOAT:
            (v,) = struct.unpack_from("<d", data, pos)
            pos += 8
        elif col.type == TYPE_BOOL:
            (v,) = struct.unpack_from("<?", data, pos)
            pos += 1
        elif col.type == TYPE_VARCHAR:
            (raw,) = struct.unpack_from(f"<{col.size}s", data, pos)
            v = raw.rstrip(b"\x00").decode("utf-8")
            pos += col.size
        elif col.type == TYPE_POINT:
            x, y = struct.unpack_from("<dd", data, pos)
            v = (x, y)
            pos += 16
        elif col.type == TYPE_TEXT:
            (n,) = struct.unpack_from("<H", data, pos)
            pos += 2
            v = data[pos : pos + n].decode("utf-8")
            pos += n
        else:
            raise SerializationError(f"tipo desconocido: {col.type}")
        values.append(v)
    return values


# ----------------------------------------------------------------------
# Codificación de claves de longitud fija para los índices
# ----------------------------------------------------------------------
def key_size(col: Column) -> int:
    """Tamaño en bytes de la clave codificada de la columna."""
    if col.type == TYPE_INT:
        return 4
    if col.type == TYPE_FLOAT:
        return 8
    if col.type == TYPE_BOOL:
        return 1
    if col.type == TYPE_VARCHAR:
        return col.size or 0
    if col.type == TYPE_TEXT:
        return MAX_TEXT_KEY
    raise SerializationError(f"la columna '{col.name}' ({col.type}) no es indexable")


def encode_key(value, col: Column) -> bytes:
    """Codifica un valor escalar como clave de longitud fija."""
    v = coerce_value(value, col)
    if col.type == TYPE_INT:
        return struct.pack("<i", v)
    if col.type == TYPE_FLOAT:
        return struct.pack("<d", v)
    if col.type == TYPE_BOOL:
        return struct.pack("<?", v)
    if col.type in (TYPE_VARCHAR, TYPE_TEXT):
        n = key_size(col)
        return struct.pack(f"<{n}s", v.encode("utf-8")[:n])
    raise SerializationError(f"la columna '{col.name}' no es indexable")


def decode_key(data: bytes, col: Column):
    """Decodifica una clave de longitud fija al valor Python."""
    if col.type == TYPE_INT:
        return struct.unpack("<i", data)[0]
    if col.type == TYPE_FLOAT:
        return struct.unpack("<d", data)[0]
    if col.type == TYPE_BOOL:
        return struct.unpack("<?", data)[0]
    if col.type in (TYPE_VARCHAR, TYPE_TEXT):
        return data.rstrip(b"\x00").decode("utf-8")
    raise SerializationError(f"la columna '{col.name}' no es indexable")
