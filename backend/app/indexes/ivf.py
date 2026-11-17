"""Índice vectorial aproximado IVF (Inverted File Flat) en disco.

Dos archivos binarios por índice (base ``{tabla}_{columna}``):

- ``{base}.cen`` — Archivo Directorio de Centroides: cabecera y una
  entrada por celda de Voronoi ``<cluster_id, head_page, tail_page,
  count, centroide[D]>``. Las celdas se construyen con K-Means
  (C = ``lists``, por defecto floor(sqrt(N))) sobre los vectores de la
  columna.
- ``{base}.lst`` — Archivo de Listas Invertidas Paginadas: páginas de
  4 KB encadenadas por celda; cada página almacena tuplas
  ``<RID, alive, vector[D]>``. Durante una consulta se eligen los
  ``probes`` centroides más cercanos al vector de consulta y se
  escanean únicamente las páginas de esas celdas (esas lecturas son
  las que contabiliza el ``DiskCounter``).

La búsqueda mantiene los k mejores candidatos en un max-heap
(estrategia Filter-and-Refine: la poda por celdas es el filtro y la
distancia exacta sobre los candidatos es el refinamiento). Métricas
soportadas: ``L2`` (euclidiana) y ``COSINE`` (1 - coseno).
"""

from __future__ import annotations

import heapq
import math
import os
import struct

import numpy as np

from ..storage.disk_counter import CountedFile, DiskCounter

PAGE_SIZE = 4096

_CEN_HEADER_FMT = "<4sIIIII"  # magic, version, dim, n_clusters, count, metric
_CEN_HEADER_SIZE = struct.calcsize(_CEN_HEADER_FMT)
_CEN_ENTRY_FMT = "<IIII"      # head_page, tail_page, count, reservado
_CEN_ENTRY_SIZE = struct.calcsize(_CEN_ENTRY_FMT)
_CEN_MAGIC = b"MVIC"

_METRIC_CODE = {"L2": 0, "COSINE": 1}

_LST_HEADER_FMT = "<4sII"     # magic, version, page_count
_LST_HEADER_SIZE = struct.calcsize(_LST_HEADER_FMT)
_LST_MAGIC = b"MVIL"

_PAGE_HEADER_FMT = "<IiHH"    # page_id, next_page, count, reservado
_PAGE_HEADER_SIZE = struct.calcsize(_PAGE_HEADER_FMT)

_ENTRY_HEADER_FMT = "<iHBB"   # rid_page, rid_slot, alive, reservado
_ENTRY_HEADER_SIZE = struct.calcsize(_ENTRY_HEADER_FMT)

METRICS = ("L2", "COSINE")


class IVFIndex:
    """IVF Flat paginado en disco.

    Con ``create=True`` se inicializa vacío; los vectores se cargan con
    ``build()`` (entrenamiento K-Means + escritura de las listas) y
    luego se mantienen con ``add``/``delete``.
    """

    def __init__(self, base_path: str, create: bool = False,
                 counter: DiskCounter | None = None) -> None:
        self.base_path = base_path
        self._counter = counter or DiskCounter()
        self._cen_path = f"{base_path}.cen"
        self._lst_path = f"{base_path}.lst"
        if create:
            with open(self._cen_path, "wb") as f:
                f.write(struct.pack(_CEN_HEADER_FMT, _CEN_MAGIC, 1,
                                    0, 0, 0, 0))
            with open(self._lst_path, "wb") as f:
                f.write(struct.pack(_LST_HEADER_FMT, _LST_MAGIC, 1, 0))
                f.write(b"\x00" * (PAGE_SIZE - _LST_HEADER_SIZE))
        self._cen = CountedFile(open(self._cen_path, "r+b"), self._counter)
        self._lst = CountedFile(open(self._lst_path, "r+b"), self._counter)

        # Directorio en memoria (las listas solo se leen bajo demanda)
        self.dim = 0
        self.count = 0            # vectores vivos aproximados (suma de celdas)
        self.centroids: np.ndarray | None = None   # (C, dim)
        self.clusters: list[list[int]] = []        # [head, tail, count]
        self.metric = "L2"
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------
    # Carga y persistencia del directorio
    # ------------------------------------------------------------------
    def _load(self) -> None:
        self._cen.seek(0)
        header = self._cen.read(_CEN_HEADER_SIZE)
        (magic, _ver, self.dim, n_clusters, self.count,
         metric_code) = struct.unpack(_CEN_HEADER_FMT, header)
        if magic != _CEN_MAGIC:
            raise ValueError(f"{self._cen_path} no es un directorio IVF")
        self.metric = next((m for m, c in _METRIC_CODE.items()
                            if c == metric_code), "L2")
        self.clusters = []
        cents = []
        for _ in range(n_clusters):
            raw = self._cen.read(_CEN_ENTRY_SIZE)
            head, tail, cnt, _res = struct.unpack(_CEN_ENTRY_FMT, raw)
            vec = struct.unpack(f"<{self.dim}f",
                                self._cen.read(4 * self.dim))
            self.clusters.append([head, tail, cnt])
            cents.append(vec)
        self.centroids = (np.array(cents, dtype=np.float32)
                          if cents else None)

    def _write_dir(self) -> None:
        self._cen.seek(0)
        self._cen.write(struct.pack(_CEN_HEADER_FMT, _CEN_MAGIC, 1,
                                    self.dim, len(self.clusters),
                                    self.count,
                                    _METRIC_CODE[self.metric]))
        for (head, tail, cnt), cent in zip(self.clusters, self.centroids):
            self._cen.write(struct.pack(_CEN_ENTRY_FMT, head, tail, cnt, 0))
            self._cen.write(struct.pack(f"<{self.dim}f", *cent.tolist()))
        self._cen.truncate()
        self._cen.flush()

    def _write_lst_header(self, page_count: int) -> None:
        self._lst.seek(0)
        self._lst.write(struct.pack(_LST_HEADER_FMT, _LST_MAGIC, 1,
                                    page_count))
        self._lst.flush()

    def _page_count(self) -> int:
        self._lst.seek(0)
        raw = self._lst.read(_LST_HEADER_SIZE)
        _magic, _ver, page_count = struct.unpack(_LST_HEADER_FMT, raw)
        return page_count

    # ------------------------------------------------------------------
    # Páginas de las listas invertidas
    # ------------------------------------------------------------------
    @property
    def _entry_size(self) -> int:
        return _ENTRY_HEADER_SIZE + 4 * self.dim

    @property
    def _page_capacity(self) -> int:
        return (PAGE_SIZE - _PAGE_HEADER_SIZE) // self._entry_size

    def _page_offset(self, page_id: int) -> int:
        return page_id * PAGE_SIZE

    def _read_page(self, page_id: int) -> tuple[int, list]:
        """Lee una página de 4 KB (1 I/O contado).

        Devuelve ``(next_page, entradas)``; cada entrada es
        ``[rid_page, rid_slot, alive, vector]``.
        """
        self._lst.seek(self._page_offset(page_id))
        raw = self._lst.read(PAGE_SIZE)
        _pid, next_page, count, _res = struct.unpack_from(
            _PAGE_HEADER_FMT, raw, 0)
        entries = []
        for i in range(count):
            off = _PAGE_HEADER_SIZE + i * self._entry_size
            rid_page, rid_slot, alive, _r = struct.unpack_from(
                _ENTRY_HEADER_FMT, raw, off)
            vec = struct.unpack_from(f"<{self.dim}f", raw,
                                     off + _ENTRY_HEADER_SIZE)
            entries.append([rid_page, rid_slot, alive, vec])
        return next_page, entries

    def _write_page(self, page_id: int, next_page: int,
                    entries: list) -> None:
        """Escribe una página completa de 4 KB (1 I/O contado)."""
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(_PAGE_HEADER_FMT, buf, 0, page_id, next_page,
                         len(entries), 0)
        for i, (rid_page, rid_slot, alive, vec) in enumerate(entries):
            off = _PAGE_HEADER_SIZE + i * self._entry_size
            struct.pack_into(_ENTRY_HEADER_FMT, buf, off,
                             rid_page, rid_slot, alive, 0)
            struct.pack_into(f"<{self.dim}f", buf,
                             off + _ENTRY_HEADER_SIZE, *vec)
        self._lst.seek(self._page_offset(page_id))
        self._lst.write(bytes(buf))

    def _alloc_page(self) -> int:
        page_count = self._page_count()
        self._write_lst_header(page_count + 1)
        return page_count

    # ------------------------------------------------------------------
    # Construcción (K-Means + escritura de listas)
    # ------------------------------------------------------------------
    def build(self, vectors: list[tuple[tuple[int, int], tuple[float, ...]]],
              dim: int, n_clusters: int, metric: str = "L2",
              seed: int = 42) -> None:
        """Construye el índice desde cero sobre ``vectors``.

        Entrena K-Means con ``n_clusters`` celdas (floor(sqrt(N)) si el
        llamador así lo decidió), asigna cada vector a su centroide más
        cercano y escribe las listas invertidas paginadas.
        """
        metric = metric.upper()
        if metric not in METRICS:
            raise ValueError(f"métrica no soportada: {metric!r}")
        if not vectors:
            raise ValueError("no se puede construir un IVF sin vectores")
        self.metric = metric
        self.dim = dim
        data = np.array([v for _rid, v in vectors], dtype=np.float32)
        n = len(data)
        c = max(1, min(n_clusters, n))
        work = _normalize(data) if metric == "COSINE" else data
        cents = _kmeans(work, c, seed=seed)

        labels = _nearest(work, cents)
        self.centroids = cents
        self.clusters = []
        self.count = n

        # Reescribe el archivo de listas: página 0 reservada para la
        # cabecera; las celdas se escriben en cadenas de páginas.
        self._lst.seek(0)
        self._lst.truncate(0)
        self._lst.write(struct.pack(_LST_HEADER_FMT, _LST_MAGIC, 1, 1))
        self._lst.write(b"\x00" * (PAGE_SIZE - _LST_HEADER_SIZE))
        page_count = 1
        capacity = self._page_capacity
        for cluster_id in range(c):
            members = [vectors[i] for i in range(n) if labels[i] == cluster_id]
            head = tail = -1
            entries: list = []
            for rid, vec in members:
                entries.append([rid[0], rid[1], 1,
                                tuple(float(x) for x in vec)])
                if len(entries) == capacity:
                    page_id = page_count
                    page_count += 1
                    if head == -1:
                        head = page_id
                    else:
                        self._link_page(tail, page_id)
                    self._write_page(page_id, -1, entries)
                    tail = page_id
                    entries = []
            if entries or head == -1:
                page_id = page_count
                page_count += 1
                if head == -1:
                    head = page_id
                else:
                    self._link_page(tail, page_id)
                self._write_page(page_id, -1, entries)
                tail = page_id
            self.clusters.append([head, tail, len(members)])
        self._write_lst_header(page_count)
        self._lst.flush()
        self._dirty = True
        self.close_dir()

    def _link_page(self, page_id: int, next_page: int) -> None:
        """Actualiza el puntero ``next`` de una página ya escrita."""
        self._lst.seek(self._page_offset(page_id) + 4)
        self._lst.write(struct.pack("<i", next_page))

    # ------------------------------------------------------------------
    # Mantenimiento incremental
    # ------------------------------------------------------------------
    def add(self, rid: tuple[int, int], vector) -> None:
        """Inserta un vector en la celda de su centroide más cercano."""
        vec = np.array(vector, dtype=np.float32)
        work = _normalize(vec.reshape(1, -1))[0] \
            if self.metric == "COSINE" else vec
        cluster_id = int(_nearest(work.reshape(1, -1),
                                  self.centroids)[0])
        head, tail, cnt = self.clusters[cluster_id]
        entry = [rid[0], rid[1], 1, tuple(float(x) for x in vector)]
        if tail == -1:  # celda vacía
            page_id = self._alloc_page()
            self._write_page(page_id, -1, [entry])
            self.clusters[cluster_id] = [page_id, page_id, 1]
        else:
            next_page, entries = self._read_page(tail)
            if len(entries) < self._page_capacity:
                entries.append(entry)
                self._write_page(tail, next_page, entries)
            else:
                page_id = self._alloc_page()
                self._write_page(page_id, -1, [entry])
                self._link_page(tail, page_id)
                self.clusters[cluster_id][1] = page_id
            self.clusters[cluster_id][2] += 1
        self.count += 1
        self._dirty = True

    def delete(self, rid: tuple[int, int], vector) -> bool:
        """Marca como muerta la entrada del RID dentro de su celda."""
        vec = np.array(vector, dtype=np.float32)
        work = _normalize(vec.reshape(1, -1))[0] \
            if self.metric == "COSINE" else vec
        cluster_id = int(_nearest(work.reshape(1, -1),
                                  self.centroids)[0])
        head, _tail, cnt = self.clusters[cluster_id]
        page_id = head
        while page_id != -1:
            next_page, entries = self._read_page(page_id)
            for entry in entries:
                if entry[2] and (entry[0], entry[1]) == (rid[0], rid[1]):
                    entry[2] = 0
                    self._write_page(page_id, next_page, entries)
                    self.clusters[cluster_id][2] = cnt - 1
                    self.count -= 1
                    self._dirty = True
                    return True
            page_id = next_page
        return False

    # ------------------------------------------------------------------
    # Búsqueda aproximada (ANN)
    # ------------------------------------------------------------------
    def search(self, query, probes: int = 1,
               k: int = 10) -> list[tuple[tuple[int, int], float]]:
        """Top-K aproximado: ``probes`` celdas más cercanas a ``query``.

        Devuelve ``[(RID, distancia)]`` ordenado ascendente. Solo se
        leen las páginas de las celdas sondeadas (I/O contado).
        """
        if self.centroids is None or len(self.clusters) == 0 or k <= 0:
            return []
        q = np.array(query, dtype=np.float32)
        work = _normalize(q.reshape(1, -1))[0] \
            if self.metric == "COSINE" else q
        dists = np.linalg.norm(self.centroids - work, axis=1)
        order = np.argsort(dists, kind="stable")
        probes = max(1, min(probes, len(self.clusters)))

        heap: list[tuple[float, tuple[int, int]]] = []  # max-heap (-dist)
        for cluster_id in order[:probes]:
            head, _tail, _cnt = self.clusters[int(cluster_id)]
            page_id = head
            while page_id != -1:
                next_page, entries = self._read_page(page_id)
                for rid_page, rid_slot, alive, vec in entries:
                    if not alive:
                        continue
                    d = _distance(q, vec, self.metric)
                    rid = (rid_page, rid_slot)
                    if len(heap) < k:
                        heapq.heappush(heap, (-d, rid))
                    elif d < -heap[0][0]:
                        heapq.heapreplace(heap, (-d, rid))
                page_id = next_page
        return sorted(((rid, -nd) for nd, rid in heap), key=lambda t: t[1])

    def close_dir(self) -> None:
        """Persiste el directorio de centroides si hubo cambios."""
        if self._dirty:
            self._write_dir()
            self._dirty = False

    def close(self) -> None:
        self.close_dir()
        self._cen.close()
        self._lst.close()


# ----------------------------------------------------------------------
# Álgebra de vectores (K-Means y distancias)
# ----------------------------------------------------------------------
def _normalize(data: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(data, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return data / norms


def _distance(q: np.ndarray, vec, metric: str) -> float:
    v = np.asarray(vec, dtype=np.float32)
    if metric == "COSINE":
        nq = float(np.linalg.norm(q))
        nv = float(np.linalg.norm(v))
        if nq == 0.0 or nv == 0.0:
            return 1.0
        return 1.0 - float(np.dot(q, v)) / (nq * nv)
    return float(np.linalg.norm(q - v))


# Alias público para el ejecutor (distancia exacta L2 o coseno).
vector_distance = _distance


def _nearest(data: np.ndarray, cents: np.ndarray) -> np.ndarray:
    """Índice del centroide más cercano (L2) para cada fila de data."""
    # ||x - c||^2 = ||x||^2 - 2 x·c + ||c||^2
    d2 = (np.sum(data * data, axis=1, keepdims=True)
          - 2.0 * data @ cents.T
          + np.sum(cents * cents, axis=1))
    return np.argmin(d2, axis=1)


def _kmeans(data: np.ndarray, c: int, seed: int = 42,
            iterations: int = 25) -> np.ndarray:
    """K-Means (Lloyd) con inicialización k-means++ sobre ``data``."""
    rng = np.random.default_rng(seed)
    n = len(data)
    cents = np.empty((c, data.shape[1]), dtype=np.float32)
    first = int(rng.integers(n))
    cents[0] = data[first]
    closest = np.sum((data - cents[0]) ** 2, axis=1)
    for i in range(1, c):
        total = float(closest.sum())
        if total == 0.0:  # todos los puntos iguales: rellenar repetidos
            cents[i:] = data[rng.integers(n, size=c - i)]
            break
        probs = closest / total
        idx = int(rng.choice(n, p=probs))
        cents[i] = data[idx]
        closest = np.minimum(closest,
                             np.sum((data - cents[i]) ** 2, axis=1))
    for _ in range(iterations):
        labels = _nearest(data, cents)
        new_cents = cents.copy()
        for j in range(c):
            members = data[labels == j]
            if len(members) > 0:
                new_cents[j] = members.mean(axis=0)
        if np.allclose(new_cents, cents, atol=1e-6):
            break
        cents = new_cents
    return cents
