"""R-Tree para datos espaciales (puntos 2D), persistido en archivo.

Estructura:

- Página 0 (cabecera): magic, página raíz, número de páginas y número
  máximo de entradas por nodo (calculado para que un nodo quepa en una
  página de 4 KB, salvo que se sobreescriba en pruebas).
- Páginas 1..N: nodos. Cada entrada es un MBR (x1, y1, x2, y2) más un RID
  (hojas) o un puntero a nodo hijo (internos).

Inserción con choose-leaf por mínima ampliación de área y split
cuadrático de Guttman. Búsqueda por radio con poda por MBR + filtro de
distancia exacta, y KNN con cola de prioridad sobre la distancia mínima
al MBR (mindist). La eliminación no reinserta nodos en underflow
(simplificación didáctica documentada).
"""

from __future__ import annotations

import heapq
import math
import os
import struct

from ..storage.page import PAGE_SIZE

MAGIC = b"RTR1"
HEADER_FMT = "<4sIIH"  # magic, root_page, page_count, max_entries
HEADER_SIZE = struct.calcsize(HEADER_FMT)

NODE_HEADER_FMT = "<BBH"  # is_leaf, reservado, count
NODE_HEADER_SIZE = struct.calcsize(NODE_HEADER_FMT)

LEAF_ENTRY_FMT = "<ddddIH"  # mbr (4 doubles) + rid (page, slot)
LEAF_ENTRY_SIZE = struct.calcsize(LEAF_ENTRY_FMT)

INTERNAL_ENTRY_FMT = "<ddddI"  # mbr + child page
INTERNAL_ENTRY_SIZE = struct.calcsize(INTERNAL_ENTRY_FMT)

RID = tuple[int, int]
Point = tuple[float, float]
MBR = tuple[float, float, float, float]


def mbr_of_point(p: Point) -> MBR:
    return (p[0], p[1], p[0], p[1])


def mbr_union(a: MBR, b: MBR) -> MBR:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def mbr_area(m: MBR) -> float:
    return max(0.0, m[2] - m[0]) * max(0.0, m[3] - m[1])


def mbr_enlargement(m: MBR, p: Point) -> float:
    return mbr_area(mbr_union(m, mbr_of_point(p))) - mbr_area(m)


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def mindist(m: MBR, p: Point) -> float:
    """Distancia mínima de un punto a un MBR (0 si está dentro)."""
    dx = max(m[0] - p[0], 0.0, p[0] - m[2])
    dy = max(m[1] - p[1], 0.0, p[1] - m[3])
    return math.hypot(dx, dy)


class _Node:
    __slots__ = ("is_leaf", "entries")

    def __init__(self, is_leaf: bool) -> None:
        self.is_leaf = is_leaf
        # hoja: [(mbr, rid)] ; interno: [(mbr, child_page)]
        self.entries: list = []


class RTree:
    """R-Tree con split cuadrático, persistido en páginas de 4 KB."""

    def __init__(self, path: str, max_entries: int | None = None,
                 create: bool = False) -> None:
        self.path = path
        default_m = max(2, (PAGE_SIZE - NODE_HEADER_SIZE) // LEAF_ENTRY_SIZE)
        if create or not os.path.exists(path):
            self.max_entries = max_entries or default_m
            if self.max_entries < 2:
                raise ValueError("max_entries debe ser >= 2")
            self.root_page = 1
            self.page_count = 2
            self._file = open(path, "w+b")
            self._store_node(self.root_page, _Node(True))
            self._write_header()
        else:
            self._file = open(path, "r+b")
            self._read_header()
            if max_entries is not None and max_entries != self.max_entries:
                raise ValueError("max_entries incompatible con el archivo")

    # ------------------------------------------------------------------
    # Cabecera y páginas
    # ------------------------------------------------------------------
    def _write_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, buf, 0, MAGIC, self.root_page, self.page_count,
            self.max_entries,
        )
        self._file.seek(0)
        self._file.write(buf)
        self._file.flush()

    def _read_header(self) -> None:
        self._file.seek(0)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != MAGIC:
            raise ValueError(f"{self.path} no es un archivo R-Tree válido")
        (_, self.root_page, self.page_count,
         self.max_entries) = struct.unpack_from(HEADER_FMT, data, 0)

    def _alloc_page(self) -> int:
        page_id = self.page_count
        self.page_count += 1
        return page_id

    def _load_node(self, page_id: int) -> _Node:
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        is_leaf, _, count = struct.unpack_from(NODE_HEADER_FMT, data, 0)
        node = _Node(bool(is_leaf))
        pos = NODE_HEADER_SIZE
        for _ in range(count):
            if node.is_leaf:
                x1, y1, x2, y2, p, s = struct.unpack_from(LEAF_ENTRY_FMT, data, pos)
                node.entries.append(((x1, y1, x2, y2), (p, s)))
                pos += LEAF_ENTRY_SIZE
            else:
                x1, y1, x2, y2, child = struct.unpack_from(
                    INTERNAL_ENTRY_FMT, data, pos
                )
                node.entries.append(((x1, y1, x2, y2), child))
                pos += INTERNAL_ENTRY_SIZE
        return node

    def _store_node(self, page_id: int, node: _Node) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            NODE_HEADER_FMT, buf, 0, 1 if node.is_leaf else 0, 0, len(node.entries)
        )
        pos = NODE_HEADER_SIZE
        for mbr, ref in node.entries:
            if node.is_leaf:
                struct.pack_into(LEAF_ENTRY_FMT, buf, pos, *mbr, *ref)
                pos += LEAF_ENTRY_SIZE
            else:
                struct.pack_into(INTERNAL_ENTRY_FMT, buf, pos, *mbr, ref)
                pos += INTERNAL_ENTRY_SIZE
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(bytes(buf))
        self._file.flush()

    # ------------------------------------------------------------------
    # Inserción con split cuadrático
    # ------------------------------------------------------------------
    def insert(self, point: Point, rid: RID) -> None:
        point = (float(point[0]), float(point[1]))
        split = self._insert(self.root_page, point, rid)
        if split is not None:
            right_page = split
            left_mbr = self._node_mbr(self.root_page)
            right_mbr = self._node_mbr(right_page)
            new_root = _Node(False)
            new_root.entries = [(left_mbr, self.root_page), (right_mbr, right_page)]
            new_page = self._alloc_page()
            self._store_node(new_page, new_root)
            self.root_page = new_page
        self._write_header()

    def _insert(self, page_id: int, point: Point, rid: RID) -> int | None:
        """Inserta y devuelve la página del nuevo hermano si hubo split."""
        node = self._load_node(page_id)
        if node.is_leaf:
            node.entries.append((mbr_of_point(point), rid))
        else:
            idx = min(
                range(len(node.entries)),
                key=lambda i: (
                    mbr_enlargement(node.entries[i][0], point),
                    mbr_area(node.entries[i][0]),
                ),
            )
            mbr, child = node.entries[idx]
            split = self._insert(child, point, rid)
            node.entries[idx] = (mbr_union(mbr, mbr_of_point(point)), child)
            if split is not None:
                node.entries.append((self._node_mbr(split), split))
        if len(node.entries) <= self.max_entries:
            self._store_node(page_id, node)
            return None
        left, right = self._quadratic_split(node)
        new_page = self._alloc_page()
        self._store_node(page_id, left)
        self._store_node(new_page, right)
        return new_page

    def _node_mbr(self, page_id: int) -> MBR:
        node = self._load_node(page_id)
        mbr = node.entries[0][0]
        for e in node.entries[1:]:
            mbr = mbr_union(mbr, e[0])
        return mbr

    def _quadratic_split(self, node: _Node) -> tuple[_Node, _Node]:
        """Split cuadrático de Guttman sobre las entradas del nodo."""
        entries = list(node.entries)
        # 1) pick seeds: par con mayor espacio desperdiciado
        worst, seeds = -1.0, (0, 1)
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                waste = (
                    mbr_area(mbr_union(entries[i][0], entries[j][0]))
                    - mbr_area(entries[i][0])
                    - mbr_area(entries[j][0])
                )
                if waste > worst:
                    worst, seeds = waste, (i, j)
        left = _Node(node.is_leaf)
        right = _Node(node.is_leaf)
        left.entries.append(entries[seeds[0]])
        right.entries.append(entries[seeds[1]])
        rest = [e for k, e in enumerate(entries) if k not in seeds]
        # 2) distribuir el resto
        while rest:
            if len(left.entries) + len(rest) == self.max_entries // 2 + 1:
                left.entries.extend(rest)
                break
            if len(right.entries) + len(rest) == self.max_entries // 2 + 1:
                right.entries.extend(rest)
                break
            # pick next: entrada con mayor diferencia de ampliación
            def mbr_of(group):
                m = group.entries[0][0]
                for x in group.entries[1:]:
                    m = mbr_union(m, x[0])
                return m

            def prefs(e):
                """Ampliación de área de agregar ``e`` a cada grupo."""
                lm, rm = mbr_of(left), mbr_of(right)
                if node.is_leaf:
                    p = (e[0][0], e[0][1])
                    return mbr_enlargement(lm, p), mbr_enlargement(rm, p)
                dl = mbr_area(mbr_union(lm, e[0])) - mbr_area(lm)
                dr = mbr_area(mbr_union(rm, e[0])) - mbr_area(rm)
                return dl, dr

            chosen = max(rest, key=lambda e: abs(prefs(e)[0] - prefs(e)[1]))
            rest.remove(chosen)
            dl, dr = prefs(chosen)
            if dl < dr:
                left.entries.append(chosen)
            elif dr < dl:
                right.entries.append(chosen)
            else:
                # empate: menor área, luego menor cantidad
                smaller = left if len(left.entries) <= len(right.entries) else right
                smaller.entries.append(chosen)
        return left, right

    # ------------------------------------------------------------------
    # Eliminación (sin reinserción en underflow)
    # ------------------------------------------------------------------
    def delete(self, point: Point, rid: RID) -> None:
        point = (float(point[0]), float(point[1]))
        if not self._delete(self.root_page, point, rid):
            raise KeyError(f"entrada no encontrada: {point} {rid}")
        self._write_header()

    def _delete(self, page_id: int, point: Point, rid: RID) -> bool:
        node = self._load_node(page_id)
        if node.is_leaf:
            for i, (mbr, r) in enumerate(node.entries):
                if r == rid and mbr == mbr_of_point(point):
                    del node.entries[i]
                    self._store_node(page_id, node)
                    return True
            return False
        changed = False
        for i, (mbr, child) in enumerate(node.entries):
            if mindist(mbr, point) == 0.0:
                if self._delete(child, point, rid):
                    child_node = self._load_node(child)
                    if child_node.entries:
                        # ajustar el MBR tras la eliminación
                        node.entries[i] = (self._node_mbr(child), child)
                    changed = True
                    break
        if changed:
            self._store_node(page_id, node)
        return changed

    # ------------------------------------------------------------------
    # Búsquedas espaciales
    # ------------------------------------------------------------------
    def search_radius(self, center: Point, radius: float) -> list[RID]:
        """RIDs cuyo punto está a distancia <= ``radius`` del centro.

        Poda por MBR (mindist) y filtro final de distancia exacta.
        """
        center = (float(center[0]), float(center[1]))
        results: list[RID] = []
        self._radius(self.root_page, center, float(radius), results)
        return results

    def _radius(self, page_id: int, center: Point, radius: float,
                out: list[RID]) -> None:
        node = self._load_node(page_id)
        for mbr, ref in node.entries:
            if mindist(mbr, center) > radius:
                continue  # poda por MBR
            if node.is_leaf:
                p = (mbr[0], mbr[1])
                if dist(p, center) <= radius:  # filtro exacto
                    out.append(ref)
            else:
                self._radius(ref, center, radius, out)

    def knn(self, center: Point, k: int) -> list[tuple[RID, float]]:
        """Los ``k`` puntos más cercanos: ``[(rid, distancia)]``.

        Cola de prioridad (min-heap) sobre la distancia mínima al MBR;
        las entradas de hoja se reportan al salir de la cola.
        """
        center = (float(center[0]), float(center[1]))
        heap: list = []
        seq = 0
        heapq.heappush(heap, (0.0, seq, "node", self.root_page))
        results: list[tuple[RID, float]] = []
        while heap and len(results) < k:
            d, _, kind, payload = heapq.heappop(heap)
            if kind == "entry":
                results.append((payload, d))
                continue
            node = self._load_node(payload)
            for mbr, ref in node.entries:
                seq += 1
                if node.is_leaf:
                    p = (mbr[0], mbr[1])
                    heapq.heappush(heap, (dist(p, center), seq, "entry", ref))
                else:
                    heapq.heappush(heap, (mindist(mbr, center), seq, "node", ref))
        return results

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "RTree":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
