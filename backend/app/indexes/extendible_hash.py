"""Hash extensible (extendible hashing) persistido en un archivo.

Estructura:

- Directorio con profundidad global ``g``: ``2**g`` entradas que apuntan
  a buckets, indexadas por los ``g`` bits menos significativos del hash.
- Buckets de capacidad fija (la que cabe en una página de 4 KB) con
  profundidad local. Al desbordarse un bucket con profundidad local igual
  a la global, el directorio se duplica.

Función hash: FNV-1a de 32 bits implementada a mano sobre los bytes de la
clave codificada (sin librerías externas).

El archivo se reescribe completo en cada mutación (write-through):
página(s) del directorio primero y luego una página por bucket, todo en
bloques de 4 KB.
"""

from __future__ import annotations

import os
import struct

from ..storage.page import PAGE_SIZE

MAGIC = b"EXH1"
HEADER_FMT = "<4sBII"  # magic, global_depth, num_buckets, key_size
HEADER_SIZE = struct.calcsize(HEADER_FMT)

BUCKET_HEADER_FMT = "<BH"  # local_depth, count
BUCKET_HEADER_SIZE = struct.calcsize(BUCKET_HEADER_FMT)

RID_FMT = "<IH"
RID_SIZE = struct.calcsize(RID_FMT)

RID = tuple[int, int]


def fnv1a_32(data: bytes) -> int:
    """FNV-1a de 32 bits, implementado desde cero."""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class _Bucket:
    __slots__ = ("local_depth", "keys", "rids")

    def __init__(self, local_depth: int) -> None:
        self.local_depth = local_depth
        self.keys: list = []
        self.rids: list[RID] = []


class ExtendibleHash:
    """Hash extensible con directorio y buckets persistidos en archivo."""

    def __init__(self, path: str, key_size: int, encode, decode,
                 create: bool = False) -> None:
        self.path = path
        self.key_size = key_size
        self.encode = encode
        self.decode = decode
        self.bucket_cap = max(
            2, (PAGE_SIZE - BUCKET_HEADER_SIZE) // (key_size + RID_SIZE)
        )
        if create or not os.path.exists(path):
            self.global_depth = 1
            self.buckets: list[_Bucket] = [_Bucket(1), _Bucket(1)]
            self.directory: list[int] = [0, 1]
            self._file = open(path, "w+b")
            self._flush()
        else:
            self._file = open(path, "r+b")
            self._load()

    # ------------------------------------------------------------------
    # Persistencia (reescritura completa del archivo por páginas de 4 KB)
    # ------------------------------------------------------------------
    def _flush(self) -> None:
        header = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, header, 0, MAGIC, self.global_depth,
            len(self.buckets), self.key_size,
        )
        # Directorio: entradas u32 a partir de la cabecera, usando las
        # páginas que haga falta.
        dir_bytes = struct.pack(f"<{len(self.directory)}I", *self.directory)
        dir_space = PAGE_SIZE - HEADER_SIZE
        pages: list[bytes] = []
        first = bytes(header[:HEADER_SIZE]) + dir_bytes[:dir_space]
        pages.append(first + b"\x00" * (PAGE_SIZE - len(first)))
        rest = dir_bytes[dir_space:]
        while rest:
            chunk, rest = rest[:PAGE_SIZE], rest[PAGE_SIZE:]
            pages.append(bytes(chunk) + b"\x00" * (PAGE_SIZE - len(chunk)))
        for bucket in self.buckets:
            buf = bytearray(PAGE_SIZE)
            struct.pack_into(
                BUCKET_HEADER_FMT, buf, 0, bucket.local_depth, len(bucket.keys)
            )
            pos = BUCKET_HEADER_SIZE
            for key, rid in zip(bucket.keys, bucket.rids):
                buf[pos : pos + self.key_size] = self.encode(key)
                pos += self.key_size
                struct.pack_into(RID_FMT, buf, pos, *rid)
                pos += RID_SIZE
            pages.append(bytes(buf))
        self._file.seek(0)
        self._file.truncate(0)
        for page in pages:
            self._file.write(page)
        self._file.flush()

    def _load(self) -> None:
        self._file.seek(0)
        first = self._file.read(PAGE_SIZE)
        if len(first) < PAGE_SIZE or first[:4] != MAGIC:
            raise ValueError(f"{self.path} no es un archivo hash válido")
        _, self.global_depth, num_buckets, ks = struct.unpack_from(HEADER_FMT, first, 0)
        if ks != self.key_size:
            raise ValueError("tamaño de clave incompatible con el archivo")
        dir_len = 4 * (1 << self.global_depth)
        dir_space = PAGE_SIZE - HEADER_SIZE
        raw = bytearray(first[HEADER_SIZE : HEADER_SIZE + min(dir_len, dir_space)])
        remaining = dir_len - len(raw)
        while remaining > 0:
            chunk = self._file.read(PAGE_SIZE)
            raw += chunk[:remaining]
            remaining -= len(chunk[:remaining])
        self.directory = list(struct.unpack(f"<{1 << self.global_depth}I", raw))
        self.buckets = []
        for _ in range(num_buckets):
            data = self._file.read(PAGE_SIZE)
            local_depth, count = struct.unpack_from(BUCKET_HEADER_FMT, data, 0)
            bucket = _Bucket(local_depth)
            pos = BUCKET_HEADER_SIZE
            for _ in range(count):
                key = self.decode(data[pos : pos + self.key_size])
                pos += self.key_size
                rid = struct.unpack_from(RID_FMT, data, pos)
                pos += RID_SIZE
                bucket.keys.append(key)
                bucket.rids.append(rid)
            self.buckets.append(bucket)

    # ------------------------------------------------------------------
    # Núcleo
    # ------------------------------------------------------------------
    def _hash(self, key) -> int:
        return fnv1a_32(self.encode(key))

    def _dir_index(self, h: int) -> int:
        return h & ((1 << self.global_depth) - 1)

    def _bucket_for(self, key) -> _Bucket:
        return self.buckets[self.directory[self._dir_index(self._hash(key))]]

    def insert(self, key, rid: RID) -> None:
        bucket = self._bucket_for(key)
        entry = (key, rid)
        for k, r in zip(bucket.keys, bucket.rids):
            if (k, r) == entry:
                raise KeyError(f"entrada duplicada: {key!r} {rid}")
        if len(bucket.keys) < self.bucket_cap:
            bucket.keys.append(key)
            bucket.rids.append(rid)
            self._flush()
            return

        # Overflow: split del bucket (duplicando el directorio si hace falta)
        if bucket.local_depth == self.global_depth:
            self.global_depth += 1
            self.directory = self.directory + self.directory
        bucket.local_depth += 1
        new_bucket = _Bucket(bucket.local_depth)
        self.buckets.append(new_bucket)
        new_id = len(self.buckets) - 1
        old_id = self.buckets.index(bucket)

        # Redirigir las entradas del directorio que ahora apuntan al nuevo bucket
        step = 1 << bucket.local_depth
        mask = step - 1
        bit = 1 << (bucket.local_depth - 1)
        for i, b in enumerate(self.directory):
            if b == old_id and (i & mask) & bit:
                self.directory[i] = new_id

        # Redistribuir las entradas existentes
        old_keys, old_rids = bucket.keys, bucket.rids
        bucket.keys, bucket.rids = [], []
        new_bucket.keys, new_bucket.rids = [], []
        for k, r in zip(old_keys, old_rids):
            target = self.buckets[self.directory[self._dir_index(self._hash(k))]]
            target.keys.append(k)
            target.rids.append(r)
        self.insert(key, rid)  # reintenta (puede requerir otro split)

    def search(self, key) -> list[RID]:
        """Devuelve todos los RIDs asociados a ``key``."""
        bucket = self._bucket_for(key)
        return [r for k, r in zip(bucket.keys, bucket.rids) if k == key]

    def delete(self, key, rid: RID) -> None:
        bucket = self._bucket_for(key)
        entries = list(zip(bucket.keys, bucket.rids))
        try:
            entries.remove((key, rid))
        except ValueError:
            raise KeyError(f"entrada no encontrada: {key!r} {rid}") from None
        bucket.keys = [k for k, _ in entries]
        bucket.rids = [r for _, r in entries]
        self._flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "ExtendibleHash":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
