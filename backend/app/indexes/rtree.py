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


def mbr_of_entries(entries) -> MBR:
    """MBR que cubre todos los MBR de una lista de entradas."""
    m = entries[0][0]
    for e in entries[1:]:
        m = mbr_union(m, e[0])
    return m


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
        # Modo de carga masiva: con ``defer_header`` la cabecera se vuelca
        # una sola vez al final (``flush_header``/``close``).
        self.defer_header = False
        self._header_dirty = False
        # Caché write-through de nodos decodificados (página -> nodo):
        # evita re-leer y re-decodificar todo el camino de raíz a hoja en
        # cada inserción. Todas las mutaciones pasan por ``_store_node``,
        # que actualiza la caché junto con el disco, así que nunca queda
        # desincronizada. Con tope: al llenarse se vacía completa.
        self._cache: dict[int, _Node] = {}
        self._cache_max = 1024
        # Buffer pool para carga masiva: con ``defer_flush`` los nodos
        # modificados se mantienen en memoria (pendientes de serializar)
        # y se escriben una sola vez al final (``flush_pages``/``close``),
        # en vez de re-serializar y reescribir la página (con flush) en
        # cada inserción. Con tope: al llenarse se vuelca y se vacía.
        self.defer_flush = False
        self._pending: dict[int, _Node] = {}
        self._pending_max = 4096
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
        if self.defer_header:
            self._header_dirty = True
            return
        self._flush_header()

    def _flush_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, buf, 0, MAGIC, self.root_page, self.page_count,
            self.max_entries,
        )
        self._file.seek(0)
        self._file.write(buf)
        self._file.flush()
        self._header_dirty = False

    def flush_header(self) -> None:
        """Vuelca la cabecera a disco si quedó pendiente (carga masiva)."""
        if self._header_dirty:
            self._flush_header()

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

    def _read_raw(self, page_id: int) -> bytes:
        node = self._pending.get(page_id)
        if node is not None:
            # página aún no volcada: se serializa al vuelo (raro: solo si
            # la caché de nodos se vació antes del volcado)
            return self._serialize_node(node)
        self._file.seek(page_id * PAGE_SIZE)
        return self._file.read(PAGE_SIZE)

    def _write_raw(self, page_id: int, data: bytes) -> None:
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)
        self._file.flush()

    def flush_pages(self) -> None:
        """Serializa y vuelca los nodos pendientes (carga masiva)."""
        if not self._pending:
            return
        for page_id in sorted(self._pending):
            self._file.seek(page_id * PAGE_SIZE)
            self._file.write(self._serialize_node(self._pending[page_id]))
        self._file.flush()
        self._pending.clear()

    def _load_node(self, page_id: int) -> _Node:
        node = self._cache.get(page_id)
        if node is not None:
            return node
        if len(self._cache) >= self._cache_max:
            # Al perder la caché no hace falta volcar: ``_read_raw``
            # consulta primero ``_pending`` y serializa al vuelo, así que
            # las re-lecturas siguen viendo el último estado en memoria.
            # (Volcar aquí reescribía miles de páginas cada pocas
            # inserciones y era el segundo cuello de botella en carga
            # masiva.)
            self._cache.clear()
        data = self._read_raw(page_id)
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
        self._cache[page_id] = node
        return node

    def _serialize_node(self, node: _Node) -> bytes:
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
        return bytes(buf)

    def _store_node(self, page_id: int, node: _Node) -> None:
        if len(self._cache) >= self._cache_max:
            # Igual que en _load_node: sin volcado al evictar (pendientes
            # siguen visibles vía _read_raw).
            self._cache.clear()
        self._cache[page_id] = node
        if self.defer_flush:
            if len(self._pending) >= self._pending_max:
                self.flush_pages()
            self._pending[page_id] = node
            return
        self._write_raw(page_id, self._serialize_node(node))

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
        """Split cuadrático de Guttman sobre las entradas del nodo.

        La aritmética de MBR va in-line y los MBR/áreas de cada grupo se
        actualizan incrementalmente: elegir cada entrada es un barrido O(M)
        con constante pequeña (la versión con llamadas por entrada era
        ~O(M²) por entrada y dominaba el costo de inserción).
        """
        entries = list(node.entries)
        n = len(entries)
        # 1) pick seeds: par con mayor espacio desperdiciado
        worst, seeds = -1.0, (0, 1)
        for i in range(n):
            ix1, iy1, ix2, iy2 = entries[i][0]
            ia = (ix2 - ix1) * (iy2 - iy1)
            for j in range(i + 1, n):
                jx1, jy1, jx2, jy2 = entries[j][0]
                w = ((jx2 if jx2 > ix2 else ix2) - (jx1 if jx1 < ix1 else ix1)) * \
                    ((jy2 if jy2 > iy2 else iy2) - (jy1 if jy1 < iy1 else iy1)) \
                    - ia - (jx2 - jx1) * (jy2 - jy1)
                if w > worst:
                    worst, seeds = w, (i, j)
        left = _Node(node.is_leaf)
        right = _Node(node.is_leaf)
        left.entries.append(entries[seeds[0]])
        right.entries.append(entries[seeds[1]])
        rest = [e for k, e in enumerate(entries) if k not in seeds]
        # 2) distribuir el resto
        lx1, ly1, lx2, ly2 = mbr_of_entries(left.entries)
        rx1, ry1, rx2, ry2 = mbr_of_entries(right.entries)
        la = (lx2 - lx1) * (ly2 - ly1)
        ra = (rx2 - rx1) * (ry2 - ry1)
        min_fill = self.max_entries // 2 + 1
        while rest:
            if len(left.entries) + len(rest) == min_fill:
                left.entries.extend(rest)
                break
            if len(right.entries) + len(rest) == min_fill:
                right.entries.extend(rest)
                break
            # pick next: entrada con mayor diferencia de ampliación
            best_i, best_diff = 0, -1.0
            best_dl = best_dr = 0.0
            for i, e in enumerate(rest):
                ex1, ey1, ex2, ey2 = e[0]
                ual = ((lx2 if lx2 > ex2 else ex2) - (lx1 if lx1 < ex1 else ex1)) * \
                      ((ly2 if ly2 > ey2 else ey2) - (ly1 if ly1 < ey1 else ey1))
                uar = ((rx2 if rx2 > ex2 else ex2) - (rx1 if rx1 < ex1 else ex1)) * \
                      ((ry2 if ry2 > ey2 else ey2) - (ry1 if ry1 < ey1 else ey1))
                dl = ual - la
                dr = uar - ra
                d = dl - dr if dl >= dr else dr - dl
                if d > best_diff:
                    best_diff, best_i = d, i
                    best_dl, best_dr = dl, dr
            chosen = rest.pop(best_i)          # entrada completa (mbr, ref)
            ex1, ey1, ex2, ey2 = chosen[0]
            if best_dl < best_dr:
                left.entries.append(chosen)
                side = left
            elif best_dr < best_dl:
                right.entries.append(chosen)
                side = right
            else:
                # empate: menor área, luego menor cantidad
                side = left if len(left.entries) <= len(right.entries) else right
                side.entries.append(chosen)
            # actualizar MBR y área del grupo elegido
            gx1, gy1, gx2, gy2, ga = (lx1, ly1, lx2, ly2, la) if side is left \
                else (rx1, ry1, rx2, ry2, ra)
            gx1 = gx1 if gx1 < ex1 else ex1
            gy1 = gy1 if gy1 < ey1 else ey1
            gx2 = gx2 if gx2 > ex2 else ex2
            gy2 = gy2 if gy2 > ey2 else ey2
            ga = (gx2 - gx1) * (gy2 - gy1)
            if side is left:
                lx1, ly1, lx2, ly2, la = gx1, gy1, gx2, gy2, ga
            else:
                rx1, ry1, rx2, ry2, ra = gx1, gy1, gx2, gy2, ga
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
        self.flush_pages()
        self.flush_header()
        self._cache.clear()
        self._file.close()

    def __enter__(self) -> "RTree":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
