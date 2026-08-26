"""B+ Tree persistido en su propio archivo de páginas de 4 KB.

Estructura del archivo:

- Página 0 (cabecera): magic, página raíz, número de páginas, tamaño de
  clave y capacidades calculadas para que cada nodo quepa en una página.
- Páginas 1..N: nodos internos u hojas.

Las claves duplicadas se soportan almacenando entradas ``(key, rid)``
ordenadas por la tupla completa: el RID actúa como desempate. Los
separadores de los nodos internos también son pares ``(key, rid)`` (la
primera entrada del subárbol derecho), lo que mantiene el invariante
estricto del B+ tree incluso con duplicados.

Las hojas se enlazan con un puntero ``next`` para los range search.

Operaciones: ``insert``, ``search`` (punto), ``range_search`` y
``delete``. La eliminación no fusiona nodos (se tolera underflow), una
simplificación habitual en implementaciones didácticas.
"""

from __future__ import annotations

import os
import struct
from bisect import bisect_left

from ..storage.page import PAGE_SIZE

MAGIC = b"BPT1"
HEADER_FMT = "<4sIIH"  # magic, root_page, page_count, key_size
HEADER_SIZE = struct.calcsize(HEADER_FMT)

RID_FMT = "<IH"  # page_id, slot_id
RID_SIZE = struct.calcsize(RID_FMT)

LEAF_HEADER_FMT = "<BHI"  # is_leaf, count, next_leaf
LEAF_HEADER_SIZE = struct.calcsize(LEAF_HEADER_FMT)

INTERNAL_HEADER_FMT = "<BH"  # is_leaf, count
INTERNAL_HEADER_SIZE = struct.calcsize(INTERNAL_HEADER_FMT)

RID = tuple[int, int]
INF_RID: RID = (0xFFFFFFFF, 0xFFFF)


class _Leaf:
    __slots__ = ("keys", "rids", "next")

    def __init__(self) -> None:
        self.keys: list = []
        self.rids: list[RID] = []
        self.next: int = 0  # 0 = no hay hoja siguiente


class _Internal:
    __slots__ = ("keys", "sep_rids", "children")

    def __init__(self) -> None:
        self.keys: list = []  # separadores (primer key del hijo derecho)
        self.sep_rids: list[RID] = []
        self.children: list[int] = []


class BPlusTree:
    """B+ Tree sobre archivo de páginas, con claves duplicadas.

    ``encode``/``decode`` convierten valores Python a/de bytes de
    longitud fija (ver ``storage.record.encode_key``).
    """

    def __init__(
        self,
        path: str,
        key_size: int,
        encode,
        decode,
        create: bool = False,
    ) -> None:
        self.path = path
        self.key_size = key_size
        self.encode = encode
        self.decode = decode
        # Capacidades calculadas para que un nodo quepa en 4 KB.
        self.leaf_cap = max(2, (PAGE_SIZE - LEAF_HEADER_SIZE) // (key_size + RID_SIZE))
        self.internal_cap = max(
            2, (PAGE_SIZE - INTERNAL_HEADER_SIZE - 4) // (key_size + RID_SIZE + 4)
        )
        if create or not os.path.exists(path):
            self.root_page = 1
            self.page_count = 2
            self._file = open(path, "w+b")
            leaf = _Leaf()
            self._store_node(self.root_page, leaf)
            self._write_header()
        else:
            self._file = open(path, "r+b")
            self._read_header()

    # ------------------------------------------------------------------
    # Cabecera y páginas
    # ------------------------------------------------------------------
    def _write_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, buf, 0, MAGIC, self.root_page, self.page_count, self.key_size
        )
        self._file.seek(0)
        self._file.write(buf)
        self._file.flush()

    def _read_header(self) -> None:
        self._file.seek(0)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != MAGIC:
            raise ValueError(f"{self.path} no es un archivo B+ tree válido")
        _, self.root_page, self.page_count, ks = struct.unpack_from(HEADER_FMT, data, 0)
        if ks != self.key_size:
            raise ValueError("tamaño de clave incompatible con el archivo")

    def _alloc_page(self) -> int:
        page_id = self.page_count
        self.page_count += 1
        return page_id

    def _read_raw(self, page_id: int) -> bytes:
        self._file.seek(page_id * PAGE_SIZE)
        return self._file.read(PAGE_SIZE)

    def _write_raw(self, page_id: int, data: bytes) -> None:
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)
        self._file.flush()

    # ------------------------------------------------------------------
    # Serialización de nodos
    # ------------------------------------------------------------------
    def _load_node(self, page_id: int):
        data = self._read_raw(page_id)
        is_leaf = data[0]
        if is_leaf:
            _, count, nxt = struct.unpack_from(LEAF_HEADER_FMT, data, 0)
            node = _Leaf()
            node.next = nxt
            pos = LEAF_HEADER_SIZE
            for _ in range(count):
                key = self.decode(data[pos : pos + self.key_size])
                pos += self.key_size
                rid = struct.unpack_from(RID_FMT, data, pos)
                pos += RID_SIZE
                node.keys.append(key)
                node.rids.append(rid)
            return node
        _, count = struct.unpack_from(INTERNAL_HEADER_FMT, data, 0)
        node = _Internal()
        pos = INTERNAL_HEADER_SIZE
        for _ in range(count):
            node.keys.append(self.decode(data[pos : pos + self.key_size]))
            pos += self.key_size
            node.sep_rids.append(struct.unpack_from(RID_FMT, data, pos))
            pos += RID_SIZE
        for _ in range(count + 1):
            (child,) = struct.unpack_from("<I", data, pos)
            pos += 4
            node.children.append(child)
        return node

    def _store_node(self, page_id: int, node) -> None:
        buf = bytearray(PAGE_SIZE)
        if isinstance(node, _Leaf):
            struct.pack_into(LEAF_HEADER_FMT, buf, 0, 1, len(node.keys), node.next)
            pos = LEAF_HEADER_SIZE
            for key, rid in zip(node.keys, node.rids):
                buf[pos : pos + self.key_size] = self.encode(key)
                pos += self.key_size
                struct.pack_into(RID_FMT, buf, pos, *rid)
                pos += RID_SIZE
        else:
            struct.pack_into(INTERNAL_HEADER_FMT, buf, 0, 0, len(node.keys))
            pos = INTERNAL_HEADER_SIZE
            for key, rid in zip(node.keys, node.sep_rids):
                buf[pos : pos + self.key_size] = self.encode(key)
                pos += self.key_size
                struct.pack_into(RID_FMT, buf, pos, *rid)
                pos += RID_SIZE
            for child in node.children:
                struct.pack_into("<I", buf, pos, child)
                pos += 4
        self._write_raw(page_id, bytes(buf))

    # ------------------------------------------------------------------
    # Navegación
    # ------------------------------------------------------------------
    def _child_index(self, node: _Internal, key, rid: RID) -> int:
        """Índice del hijo que contendría la entrada ``(key, rid)``."""
        i = 0
        while i < len(node.keys) and (key, rid) >= (node.keys[i], node.sep_rids[i]):
            i += 1
        return i

    def _find_leaf(self, key, rid: RID) -> int:
        page_id = self.root_page
        node = self._load_node(page_id)
        while isinstance(node, _Internal):
            page_id = node.children[self._child_index(node, key, rid)]
            node = self._load_node(page_id)
        return page_id

    # ------------------------------------------------------------------
    # Inserción
    # ------------------------------------------------------------------
    def insert(self, key, rid: RID) -> None:
        promotion = self._insert(self.root_page, key, rid)
        if promotion is not None:
            sep_key, sep_rid, right_page = promotion
            new_root = _Internal()
            new_root.keys = [sep_key]
            new_root.sep_rids = [sep_rid]
            new_root.children = [self.root_page, right_page]
            new_page = self._alloc_page()
            self._store_node(new_page, new_root)
            self.root_page = new_page
        self._write_header()

    def _insert(self, page_id: int, key, rid: RID):
        """Inserta y devuelve ``(sep_key, sep_rid, new_page)`` si hubo split."""
        node = self._load_node(page_id)
        if isinstance(node, _Leaf):
            entries = sorted(zip(node.keys, node.rids))
            pos = bisect_left(entries, (key, rid))
            if pos < len(entries) and entries[pos] == (key, rid):
                raise KeyError(f"entrada duplicada: {key!r} {rid}")
            entries.insert(pos, (key, rid))
            node.keys = [k for k, _ in entries]
            node.rids = [r for _, r in entries]
            if len(node.keys) <= self.leaf_cap:
                self._store_node(page_id, node)
                return None
            # split de hoja
            mid = len(node.keys) // 2
            right = _Leaf()
            right.keys = node.keys[mid:]
            right.rids = node.rids[mid:]
            right.next = node.next
            node.keys = node.keys[:mid]
            node.rids = node.rids[:mid]
            new_page = self._alloc_page()
            node.next = new_page
            self._store_node(page_id, node)
            self._store_node(new_page, right)
            return (right.keys[0], right.rids[0], new_page)

        idx = self._child_index(node, key, rid)
        result = self._insert(node.children[idx], key, rid)
        if result is None:
            return None
        sep_key, sep_rid, new_child = result
        node.keys.insert(idx, sep_key)
        node.sep_rids.insert(idx, sep_rid)
        node.children.insert(idx + 1, new_child)
        if len(node.keys) <= self.internal_cap:
            self._store_node(page_id, node)
            return None
        # split de nodo interno: la clave del medio sube
        mid = len(node.keys) // 2
        up_key, up_rid = node.keys[mid], node.sep_rids[mid]
        right = _Internal()
        right.keys = node.keys[mid + 1 :]
        right.sep_rids = node.sep_rids[mid + 1 :]
        right.children = node.children[mid + 1 :]
        node.keys = node.keys[:mid]
        node.sep_rids = node.sep_rids[:mid]
        node.children = node.children[: mid + 1]
        new_page = self._alloc_page()
        self._store_node(page_id, node)
        self._store_node(new_page, right)
        return (up_key, up_rid, new_page)

    # ------------------------------------------------------------------
    # Búsquedas
    # ------------------------------------------------------------------
    def search(self, key) -> list[RID]:
        """Devuelve todos los RIDs asociados a ``key``.

        Desciende a la hoja más a la izquierda que pueda contener la
        clave y avanza por la cadena de hojas: los duplicados pueden
        empezar en la hoja siguiente aunque la actual no los contenga.
        """
        results: list[RID] = []
        page_id = self._find_leaf(key, (0, 0))
        while page_id:
            leaf = self._load_node(page_id)
            for k, rid in zip(leaf.keys, leaf.rids):
                if k == key:
                    results.append(rid)
            if leaf.keys and leaf.keys[-1] > key:
                break  # todo lo que sigue es mayor que la clave
            page_id = leaf.next
        return results

    def _leftmost_leaf(self) -> int:
        page_id = self.root_page
        node = self._load_node(page_id)
        while isinstance(node, _Internal):
            page_id = node.children[0]
            node = self._load_node(page_id)
        return page_id

    def range_search(self, lo=None, hi=None, lo_inc: bool = True,
                     hi_inc: bool = True) -> list[RID]:
        """RIDs con clave en ``[lo, hi]``; extremos ``None`` = abiertos.

        ``lo_inc``/``hi_inc`` controlan si los extremos son inclusivos,
        lo que permite expresar ``<``, ``<=``, ``>`` y ``>=``.
        """
        results: list[RID] = []
        page_id = (self._leftmost_leaf() if lo is None
                   else self._find_leaf(lo, (0, 0)))
        while page_id:
            leaf = self._load_node(page_id)
            done = False
            for k, rid in zip(leaf.keys, leaf.rids):
                if hi is not None and (k > hi or (k == hi and not hi_inc)):
                    done = True
                    break
                if lo is not None and (k < lo or (k == lo and not lo_inc)):
                    continue
                results.append(rid)
            if done:
                break
            page_id = leaf.next
        return results

    # ------------------------------------------------------------------
    # Eliminación (sin fusión de nodos: se tolera underflow)
    # ------------------------------------------------------------------
    def delete(self, key, rid: RID) -> None:
        page_id = self._find_leaf(key, rid)
        leaf = self._load_node(page_id)
        entries = list(zip(leaf.keys, leaf.rids))
        try:
            entries.remove((key, rid))
        except ValueError:
            raise KeyError(f"entrada no encontrada: {key!r} {rid}") from None
        leaf.keys = [k for k, _ in entries]
        leaf.rids = [r for _, r in entries]
        self._store_node(page_id, leaf)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "BPlusTree":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
