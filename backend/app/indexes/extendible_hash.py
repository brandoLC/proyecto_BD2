"""Hash extensible (extendible hashing) con acceso perezoso por páginas.

Formato EXH3 del archivo (páginas de 4 KB):

- Página 0: cabecera (magic, global_depth, num_buckets, key_size).
- Páginas 1..1024: DIRECTORIO con capacidad reservada fija para
  ``2**MAX_GLOBAL_DEPTH`` entradas u32. La reserva se hace con
  ``truncate()`` al crear el archivo (región sparse: no ocupa bloques
  físicos hasta que la página se escribe de verdad), de modo que el
  directorio puede DUPLICARSE sin mover los buckets — decisión de
  diseño frente a las alternativas (directorio al final del archivo o
  en archivo aparte): el ``bucket_id`` ES la posición física del bucket
  y nunca cambia.
- Página ``BUCKET_PAGE_BASE + bucket_id``: una página por bucket, en
  offset fijo. Los ``bucket_id`` se asignan en orden creciente
  (``num_buckets`` actúa de puntero de arena); un ``bucket_id``
  consumido nunca se reutiliza.

Manejo de duplicados (buckets de overflow encadenados):

Un split solo es útil si separa las claves del bucket; cuando TODAS
las claves de un bucket lleno comparten los mismos
``MAX_GLOBAL_DEPTH`` bits bajos de su hash (en la práctica: la misma
clave repetida más veces que la capacidad del bucket), ningún split
las separaría y duplicar el directorio solo lo haría crecer hasta el
tope. En ese caso, en vez de dividir, el bucket encadena páginas de
OVERFLOW: la cabecera de cada bucket lleva un puntero ``next``
(bucket_id del siguiente eslabón, o ``NO_OVERFLOW``) y el duplicado se
inserta en el primer eslabón con espacio (o en una página nueva al
final de la cadena). Las páginas de overflow no aparecen en el
directorio: solo se alcanzan siguiendo la cadena desde el bucket
primario. ``search`` y ``delete`` recorren la cadena completa; un
bucket de overflow que queda totalmente vacío tras un borrado se
DESENLAZA de la cadena, pero su página no se reutiliza (los
``bucket_id`` no se reciclan) hasta recrear el índice.

Coste por operación (instancia fría): una búsqueda puntual lee la
cabecera + 1 página de directorio + las páginas de la cadena del
bucket (1 si no hay overflow); una inserción reescribe solo la página
del bucket afectado (+ las páginas de directorio que cambian en un
split, y la cabecera cuando cambia la profundidad o el número de
buckets).

Los formatos EXH1 (cargaba todo el archivo a memoria al abrir y lo
reescribía completo en cada mutación) y EXH2 (sin overflow: los
duplicados concentrados agotaban la profundidad global) NO se
soportan: los índices hash existentes deben recrearse (DROP TABLE /
CREATE INDEX).

Función hash: FNV-1a de 32 bits implementada a mano sobre los bytes de
la clave codificada (sin librerías externas).
"""

from __future__ import annotations

import os
import struct

from ..storage.disk_counter import CountedFile, DiskCounter
from ..storage.page import PAGE_SIZE

MAGIC = b"EXH3"
OLD_MAGICS = (b"EXH1", b"EXH2")
HEADER_FMT = "<4sBII"  # magic, global_depth, num_buckets, key_size
HEADER_SIZE = struct.calcsize(HEADER_FMT)

BUCKET_HEADER_FMT = "<BHI"  # local_depth, count, next (cadena de overflow)
BUCKET_HEADER_SIZE = struct.calcsize(BUCKET_HEADER_FMT)

# Centinela de fin de cadena de overflow (bucket_id 0 es válido).
NO_OVERFLOW = 0xFFFFFFFF

RID_FMT = "<IH"
RID_SIZE = struct.calcsize(RID_FMT)

RID = tuple[int, int]

# Profundidad global máxima: fija el tamaño reservado del directorio.
MAX_GLOBAL_DEPTH = 20
DIR_ENTRIES_PER_PAGE = PAGE_SIZE // 4  # 1024 entradas u32 por página
DIR_PAGES = (1 << MAX_GLOBAL_DEPTH) // DIR_ENTRIES_PER_PAGE  # 1024
DIR_ENTRY_FMT = f"<{DIR_ENTRIES_PER_PAGE}I"
# Página física del bucket 0 (tras cabecera + directorio reservado).
BUCKET_PAGE_BASE = 1 + DIR_PAGES


def fnv1a_32(data: bytes) -> int:
    """FNV-1a de 32 bits, implementado desde cero."""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


class _Bucket:
    __slots__ = ("local_depth", "keys", "rids", "next")

    def __init__(self, local_depth: int) -> None:
        self.local_depth = local_depth
        self.keys: list = []
        self.rids: list[RID] = []
        self.next = NO_OVERFLOW


class ExtendibleHash:
    """Hash extensible con directorio y buckets paginados bajo demanda.

    Cachés en memoria (write-back solo con ``defer_flush``; en modo
    normal las páginas sucias se escriben al final de cada operación):
    páginas de directorio decodificadas y buckets decodificados, ambas
    con tope — al llenarse se vuelcan las sucias y se vacían.
    """

    def __init__(self, path: str, key_size: int, encode, decode,
                 create: bool = False,
                 counter: DiskCounter | None = None) -> None:
        self.path = path
        self.key_size = key_size
        self.encode = encode
        self.decode = decode
        # Modo de carga masiva: con ``defer_flush`` las páginas sucias
        # (buckets, páginas de directorio, cabecera) se vuelcan una sola
        # vez al final (``flush``/``close``) en vez de por operación.
        self.defer_flush = False
        self._header_dirty = False
        self._dirty_dir: set[int] = set()
        self._dirty_buckets: set[int] = set()
        self._dir_cache: dict[int, list[int]] = {}
        self._bucket_cache: dict[int, _Bucket] = {}
        self._dir_cache_max = 64
        self._bucket_cache_max = 1024
        self.bucket_cap = max(
            2, (PAGE_SIZE - BUCKET_HEADER_SIZE) // (key_size + RID_SIZE)
        )
        if create or not os.path.exists(path):
            self.global_depth = 1
            self.num_buckets = 2
            self._file = open(path, "w+b")
            if counter is not None:
                self._file = CountedFile(self._file, counter)
            # Reserva sparse del directorio completo (ver docstring).
            self._file.truncate(BUCKET_PAGE_BASE * PAGE_SIZE)
            self._dir_set(0, 0)
            self._dir_set(1, 1)
            self._store_bucket(0, _Bucket(1))
            self._store_bucket(1, _Bucket(1))
            self._header_dirty = True
            self.flush()
        else:
            self._file = open(path, "r+b")
            if counter is not None:
                self._file = CountedFile(self._file, counter)
            self._load_header()

    # ------------------------------------------------------------------
    # Cabecera (página 0)
    # ------------------------------------------------------------------
    def _load_header(self) -> None:
        self._file.seek(0)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != MAGIC:
            if data[:4] in OLD_MAGICS:
                raise ValueError(
                    f"{self.path} usa el formato {data[:4].decode()}, que "
                    f"ya no se soporta; recrea el índice para migrarlo a "
                    f"{MAGIC.decode()}")
            raise ValueError(f"{self.path} no es un archivo hash válido")
        _, self.global_depth, self.num_buckets, ks = struct.unpack_from(
            HEADER_FMT, data, 0)
        if ks != self.key_size:
            raise ValueError("tamaño de clave incompatible con el archivo")

    def _flush_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, buf, 0, MAGIC, self.global_depth,
            self.num_buckets, self.key_size,
        )
        self._file.seek(0)
        self._file.write(buf)
        self._file.flush()
        self._header_dirty = False

    # ------------------------------------------------------------------
    # Directorio paginado (páginas 1..DIR_PAGES)
    # ------------------------------------------------------------------
    def _dir_page_id(self, idx: int) -> int:
        return 1 + idx // DIR_ENTRIES_PER_PAGE

    def _read_dir_page(self, page_id: int) -> list[int]:
        entries = self._dir_cache.get(page_id)
        if entries is None:
            if len(self._dir_cache) >= self._dir_cache_max:
                self._evict_dir()
            self._file.seek(page_id * PAGE_SIZE)
            data = self._file.read(PAGE_SIZE)
            if len(data) < PAGE_SIZE:
                # Página de la reserva sparse aún no escrita: todo ceros.
                data = data + bytes(PAGE_SIZE - len(data))
            entries = list(struct.unpack(DIR_ENTRY_FMT, data))
            self._dir_cache[page_id] = entries
        return entries

    def _evict_dir(self) -> None:
        for pid in sorted(self._dirty_dir):
            self._write_dir_page(pid)
        self._dirty_dir.clear()
        self._dir_cache.clear()

    def _dir_get(self, idx: int) -> int:
        page = self._read_dir_page(self._dir_page_id(idx))
        return page[idx % DIR_ENTRIES_PER_PAGE]

    def _dir_set(self, idx: int, bucket_id: int) -> None:
        page_id = self._dir_page_id(idx)
        self._read_dir_page(page_id)[idx % DIR_ENTRIES_PER_PAGE] = bucket_id
        self._dirty_dir.add(page_id)

    def _write_dir_page(self, page_id: int) -> None:
        buf = struct.pack(DIR_ENTRY_FMT, *self._dir_cache[page_id])
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(buf)

    # ------------------------------------------------------------------
    # Buckets en offset fijo
    # ------------------------------------------------------------------
    def _bucket_page_id(self, bucket_id: int) -> int:
        return BUCKET_PAGE_BASE + bucket_id

    def _read_bucket(self, bucket_id: int) -> _Bucket:
        bucket = self._bucket_cache.get(bucket_id)
        if bucket is None:
            if len(self._bucket_cache) >= self._bucket_cache_max:
                self._evict_buckets()
            self._file.seek(self._bucket_page_id(bucket_id) * PAGE_SIZE)
            data = self._file.read(PAGE_SIZE)
            local_depth, count, next_id = struct.unpack_from(
                BUCKET_HEADER_FMT, data, 0)
            bucket = _Bucket(local_depth)
            bucket.next = next_id
            pos = BUCKET_HEADER_SIZE
            for _ in range(count):
                bucket.keys.append(self.decode(data[pos : pos + self.key_size]))
                pos += self.key_size
                rid = struct.unpack_from(RID_FMT, data, pos)
                pos += RID_SIZE
                bucket.rids.append(rid)
            self._bucket_cache[bucket_id] = bucket
        return bucket

    def _store_bucket(self, bucket_id: int, bucket: _Bucket) -> None:
        """Marca el bucket (en caché) como pendiente de escritura."""
        self._bucket_cache[bucket_id] = bucket
        self._dirty_buckets.add(bucket_id)

    def _write_bucket(self, bucket_id: int) -> None:
        bucket = self._bucket_cache[bucket_id]
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            BUCKET_HEADER_FMT, buf, 0, bucket.local_depth,
            len(bucket.keys), bucket.next)
        pos = BUCKET_HEADER_SIZE
        for key, rid in zip(bucket.keys, bucket.rids):
            buf[pos : pos + self.key_size] = self.encode(key)
            pos += self.key_size
            struct.pack_into(RID_FMT, buf, pos, *rid)
            pos += RID_SIZE
        self._file.seek(self._bucket_page_id(bucket_id) * PAGE_SIZE)
        self._file.write(buf)

    def _evict_buckets(self) -> None:
        for bid in sorted(self._dirty_buckets):
            self._write_bucket(bid)
        self._dirty_buckets.clear()
        self._bucket_cache.clear()

    # ------------------------------------------------------------------
    # Volcado
    # ------------------------------------------------------------------
    def _commit(self) -> None:
        """Persiste las páginas sucias de la operación, o las acumula si
        el volcado está diferido (carga masiva)."""
        if not self.defer_flush:
            self.flush()

    def flush(self) -> None:
        """Vuelca a disco las páginas sucias (solo las afectadas)."""
        wrote = False
        if self._header_dirty:
            self._flush_header()
            wrote = True
        for pid in sorted(self._dirty_dir):
            self._write_dir_page(pid)
            wrote = True
        for bid in sorted(self._dirty_buckets):
            self._write_bucket(bid)
            wrote = True
        self._dirty_dir.clear()
        self._dirty_buckets.clear()
        if wrote:
            self._file.flush()

    # ------------------------------------------------------------------
    # Núcleo
    # ------------------------------------------------------------------
    def _hash(self, key) -> int:
        return fnv1a_32(self.encode(key))

    def _dir_index(self, h: int) -> int:
        return h & ((1 << self.global_depth) - 1)

    def _bucket_id_for(self, key) -> int:
        return self._dir_get(self._dir_index(self._hash(key)))

    def _chain(self, bucket_id: int):
        """Itera ``(bucket_id, bucket)`` desde el bucket primario hasta el
        final de su cadena de overflow (un solo eslabón si no hay)."""
        bid = bucket_id
        while bid != NO_OVERFLOW:
            bucket = self._read_bucket(bid)
            yield bid, bucket
            bid = bucket.next

    def _separable(self, bucket: _Bucket) -> bool:
        """True si algún split puede repartir las claves del bucket.

        Si todas las claves comparten los ``MAX_GLOBAL_DEPTH`` bits bajos
        de su hash (el caso típico: la misma clave repetida), ninguna
        profundidad las separa y dividir solo duplicaría el directorio.
        """
        p0 = self._hash(bucket.keys[0]) & ((1 << MAX_GLOBAL_DEPTH) - 1)
        return any(
            (self._hash(k) & ((1 << MAX_GLOBAL_DEPTH) - 1)) != p0
            for k in bucket.keys[1:])

    def _alloc_overflow(self) -> tuple[int, _Bucket]:
        """Aloca una página de overflow con el mismo mecanismo de arena
        que los buckets de un split (``num_buckets`` como puntero)."""
        new_id = self.num_buckets
        self.num_buckets += 1
        self._header_dirty = True
        return new_id, _Bucket(0)

    def _insert_overflow(self, bucket_id: int, key, rid: RID) -> None:
        """Inserta en la cadena de overflow del bucket lleno ``bucket_id``:
        en el primer eslabón con espacio, o en una página nueva al final."""
        for bid, bucket in self._chain(bucket_id):
            if len(bucket.keys) < self.bucket_cap:
                bucket.keys.append(key)
                bucket.rids.append(rid)
                self._store_bucket(bid, bucket)
                self._commit()
                return
            tail_id, tail = bid, bucket
        new_id, new_bucket = self._alloc_overflow()
        new_bucket.keys.append(key)
        new_bucket.rids.append(rid)
        tail.next = new_id
        self._store_bucket(tail_id, tail)
        self._store_bucket(new_id, new_bucket)
        self._commit()

    def _spill_excess(self, bucket_id: int, bucket: _Bucket) -> None:
        """Mueve a páginas de overflow encadenadas las entradas que
        exceden la capacidad del bucket (tras redistribuir un split)."""
        bid, b = bucket_id, bucket
        while len(b.keys) > self.bucket_cap:
            new_id, ovf = self._alloc_overflow()
            ovf.next = b.next
            ovf.keys = b.keys[self.bucket_cap:]
            ovf.rids = b.rids[self.bucket_cap:]
            del b.keys[self.bucket_cap:]
            del b.rids[self.bucket_cap:]
            b.next = new_id
            self._store_bucket(bid, b)
            self._store_bucket(new_id, ovf)
            bid, b = new_id, ovf

    def insert(self, key, rid: RID) -> None:
        entry = (key, rid)
        idx = self._dir_index(self._hash(key))
        bucket_id = self._dir_get(idx)
        bucket = self._read_bucket(bucket_id)
        # La entrada exacta no debe repetirse en NINGÚN eslabón de la cadena.
        for _bid, b in self._chain(bucket_id):
            for k, r in zip(b.keys, b.rids):
                if (k, r) == entry:
                    raise KeyError(f"entrada duplicada: {key!r} {rid}")
        if len(bucket.keys) < self.bucket_cap:
            bucket.keys.append(key)
            bucket.rids.append(rid)
            self._store_bucket(bucket_id, bucket)
            self._commit()
            return

        if not self._separable(bucket):
            # Bucket lleno de claves con el mismo prefijo de hash (típicamente
            # la MISMA clave): el split no las separaría y el directorio
            # crecería hasta el tope. Cadena de overflow en su lugar.
            self._insert_overflow(bucket_id, key, rid)
            return

        # Overflow: split del bucket (duplicando el directorio si hace falta)
        self._split(idx, bucket_id, bucket)
        self.insert(key, rid)  # reintenta (puede requerir otro split)

    def _split(self, idx: int, bucket_id: int, bucket: _Bucket) -> None:
        if bucket.local_depth == self.global_depth:
            self._double_directory()
        bucket.local_depth += 1
        d = bucket.local_depth
        new_id = self.num_buckets
        self.num_buckets += 1
        self._header_dirty = True
        new_bucket = _Bucket(d)

        # Redirigir al bucket nuevo las entradas del directorio con el
        # bit d-1 del patrón en alto (iteración con zancada ``step``:
        # solo se tocan las páginas de directorio que cambian).
        step = 1 << d
        bit = 1 << (d - 1)
        pattern = idx & (bit - 1)
        for i in range(pattern | bit, 1 << self.global_depth, step):
            self._dir_set(i, new_id)

        # Redistribuir las entradas de TODA la cadena (primario + overflows,
        # si el bucket encadenó duplicados antes de volverse separable)
        # entre el bucket viejo y el nuevo; las páginas de overflow viejas
        # quedan desenlazadas (su espacio no se reutiliza).
        old_entries = []
        for _bid, b in self._chain(bucket_id):
            old_entries.extend(zip(b.keys, b.rids))
        bucket.keys, bucket.rids = [], []
        bucket.next = NO_OVERFLOW
        for k, r in old_entries:
            target = (new_bucket if self._bucket_id_for(k) == new_id
                      else bucket)
            target.keys.append(k)
            target.rids.append(r)
        self._store_bucket(bucket_id, bucket)
        self._store_bucket(new_id, new_bucket)
        # Si un lado recibió más entradas de su capacidad (duplicados
        # concentrados), el excedente vuelve a una cadena de overflow.
        self._spill_excess(bucket_id, bucket)
        self._spill_excess(new_id, new_bucket)
        self._commit()

    def _double_directory(self) -> None:
        if self.global_depth >= MAX_GLOBAL_DEPTH:
            raise OverflowError(
                f"profundidad global máxima alcanzada ({MAX_GLOBAL_DEPTH})")
        old_len = 1 << self.global_depth
        self.global_depth += 1
        self._header_dirty = True
        # La mitad baja del directorio no cambia; la mitad alta es copia.
        for i in range(old_len):
            self._dir_set(old_len + i, self._dir_get(i))

    def search(self, key) -> list[RID]:
        """Devuelve todos los RIDs asociados a ``key`` (recorre la cadena
        de overflow completa del bucket)."""
        out: list[RID] = []
        for _bid, bucket in self._chain(self._bucket_id_for(key)):
            out.extend(r for k, r in zip(bucket.keys, bucket.rids)
                       if k == key)
        return out

    def delete(self, key, rid: RID) -> None:
        prev_id = NO_OVERFLOW
        prev = None
        for bid, bucket in self._chain(self._bucket_id_for(key)):
            entries = list(zip(bucket.keys, bucket.rids))
            try:
                entries.remove((key, rid))
            except ValueError:
                prev_id, prev = bid, bucket
                continue
            bucket.keys = [k for k, _ in entries]
            bucket.rids = [r for _, r in entries]
            self._store_bucket(bid, bucket)
            if prev is not None and not bucket.keys:
                # Un bucket de OVERFLOW que queda vacío se desenlaza de la
                # cadena (el primario no: el directorio apunta a él). La
                # página desenlazada no se reutiliza hasta recrear el
                # índice: los bucket_id no se reciclan.
                prev.next = bucket.next
                self._store_bucket(prev_id, prev)
            self._commit()
            return
        raise KeyError(f"entrada no encontrada: {key!r} {rid}")

    def close(self) -> None:
        self.flush()
        self._file.close()

    def __enter__(self) -> "ExtendibleHash":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
