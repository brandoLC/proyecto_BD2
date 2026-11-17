"""Índice invertido persistido en memoria secundaria.

Cuatro archivos binarios por índice (base ``{tabla}_{columna}``):

- ``{base}.dic``  — Archivo de Diccionario: cabecera + entradas de
  tamaño fijo ordenadas por término ``(term, df, offset, length)``.
  El offset apunta a la lista de posteo dentro de ``.inv``.
- ``{base}.inv``  — Archivo de Listas de Posteo: secuencia de listas
  ``<doc_id, tf>`` ordenadas ascendentemente por ``doc_id``. En
  consulta se recupera cada lista con un único ``seek(offset)``.
- ``{base}.docs`` — Mapa de documentos: ``doc_id`` (posición fija) ->
  ``(page_id, slot, alive)``. El ``doc_id`` es monótono y no se
  reutiliza, así un RID reciclado por el heap file nunca colisiona
  con un documento eliminado.
- ``{base}.nrm``  — Normas euclidianas L2 precalculadas por documento
  (``float32`` indexado por ``doc_id``), para la similitud de coseno.

Estrategia de escritura: las inserciones se acumulan en un delta en
memoria y ``flush()`` fusiona cada lista tocada (lee la lista vieja
del ``.inv``, mezcla ordenada, reescribe al final del archivo y
actualiza la entrada del diccionario). Las eliminaciones son lápidas
(tombstone en ``.docs``): las listas se purgan cuando el flush las
reescribe, y las consultas siempre filtran documentos muertos.

El diccionario se carga en RAM al abrir (como en los sistemas IR
clásicos); el archivo ``.dic`` es la fuente persistente. La carga
inicial de ``.dic/.docs/.nrm`` se hace en bloques de 4 KB
contabilizados por el ``DiskCounter``, y las listas de posteo se leen
bajo demanda (un ``seek`` + ``read`` contado por lista).
"""

from __future__ import annotations

import heapq
import math
import os
import struct

from ..storage.disk_counter import CountedFile, DiskCounter

_POSTING_FMT = "<II"          # doc_id, tf
_POSTING_SIZE = struct.calcsize(_POSTING_FMT)

_DIC_HEADER_FMT = "<4sIII"    # magic, version, term_count, live_docs
_DIC_HEADER_SIZE = struct.calcsize(_DIC_HEADER_FMT)
_DIC_ENTRY_FMT = "<32sIqI"    # term, df, offset, length
_DIC_ENTRY_SIZE = struct.calcsize(_DIC_ENTRY_FMT)
_DIC_MAGIC = b"MVID"

_DOCS_HEADER_FMT = "<4sII"    # magic, version, doc_count
_DOCS_HEADER_SIZE = struct.calcsize(_DOCS_HEADER_FMT)
_DOCS_ENTRY_FMT = "<iHBB"     # page_id, slot, alive, reservado
_DOCS_ENTRY_SIZE = struct.calcsize(_DOCS_ENTRY_FMT)
_DOCS_MAGIC = b"MVDC"

_NRM_HEADER_FMT = "<4sII"     # magic, version, doc_count
_NRM_HEADER_SIZE = struct.calcsize(_NRM_HEADER_FMT)
_NRM_MAGIC = b"MVNM"

_MAX_TERM = 32                # bytes UTF-8 por término (se trunca)


class InvertedIndex:
    """Índice invertido en disco sobre los archivos ``.dic/.inv/.docs/.nrm``.

    Uso típico::

        idx = InvertedIndex(base, create=True, counter=counter)
        idx.add_document(rid, terms)      # acumula en el delta
        idx.flush()                       # fusiona y persiste
        idx.close()

    Consultas: ``postings(term)``, ``search_boolean(ast, preprocessor)``
    y ``cosine_topk(query_terms, k)``.
    """

    def __init__(self, base_path: str, create: bool = False,
                 counter: DiskCounter | None = None) -> None:
        self.base_path = base_path
        self._counter = counter or DiskCounter()
        paths = {ext: f"{base_path}.{ext}" for ext in
                 ("dic", "inv", "docs", "nrm")}
        self._paths = paths
        if create:
            with open(paths["dic"], "wb") as f:
                f.write(struct.pack(_DIC_HEADER_FMT, _DIC_MAGIC, 1, 0, 0))
            with open(paths["inv"], "wb"):
                pass
            with open(paths["docs"], "wb") as f:
                f.write(struct.pack(_DOCS_HEADER_FMT, _DOCS_MAGIC, 1, 0))
            with open(paths["nrm"], "wb") as f:
                f.write(struct.pack(_NRM_HEADER_FMT, _NRM_MAGIC, 1, 0))
        self._dic = CountedFile(open(paths["dic"], "r+b"), self._counter)
        self._inv = CountedFile(open(paths["inv"], "r+b"), self._counter)
        self._docs = CountedFile(open(paths["docs"], "r+b"), self._counter)
        self._nrm = CountedFile(open(paths["nrm"], "r+b"), self._counter)

        # Estado en memoria (caché del diccionario y del mapa de docs)
        self.vocab: dict[str, list[int]] = {}  # term -> [df, offset, length]
        self.docs: list[list[int]] = []        # doc_id -> [page_id, slot, alive]
        self.rid_to_doc: dict[tuple[int, int], int] = {}
        self.norms: list[float] = []
        self.live_docs = 0
        self._delta: dict[str, list[tuple[int, int]]] = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------
    # Carga y persistencia del diccionario / docs / normas
    # ------------------------------------------------------------------
    def _read_blocks(self, f: CountedFile, size: int) -> bytes:
        """Lee ``size`` bytes en bloques de 4 KB.

        Cada ``read()`` de un bloque queda contabilizado por el
        DiskCounter, manteniendo la convención del motor (1 llamada =
        1 bloque) al cargar cuerpos de archivo grandes.
        """
        from ..storage.page import PAGE_SIZE
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = f.read(min(PAGE_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _load(self) -> None:
        self._dic.seek(0)
        header = self._dic.read(_DIC_HEADER_SIZE)
        magic, _ver, term_count, self.live_docs = struct.unpack(
            _DIC_HEADER_FMT, header)
        if magic != _DIC_MAGIC:
            raise ValueError(f"{self._paths['dic']} no es un diccionario válido")
        body = self._read_blocks(self._dic, term_count * _DIC_ENTRY_SIZE)
        for i in range(term_count):
            term_raw, df, offset, length = struct.unpack_from(
                _DIC_ENTRY_FMT, body, i * _DIC_ENTRY_SIZE)
            term = term_raw.rstrip(b"\x00").decode("utf-8")
            self.vocab[term] = [df, offset, length]

        self._docs.seek(0)
        header = self._docs.read(_DOCS_HEADER_SIZE)
        magic, _ver, doc_count = struct.unpack(_DOCS_HEADER_FMT, header)
        if magic != _DOCS_MAGIC:
            raise ValueError(f"{self._paths['docs']} no es un mapa válido")
        body = self._read_blocks(self._docs, doc_count * _DOCS_ENTRY_SIZE)
        for doc_id in range(doc_count):
            page_id, slot, alive, _ = struct.unpack_from(
                _DOCS_ENTRY_FMT, body, doc_id * _DOCS_ENTRY_SIZE)
            self.docs.append([page_id, slot, alive])
            if alive:
                self.rid_to_doc[(page_id, slot)] = doc_id

        self._nrm.seek(0)
        header = self._nrm.read(_NRM_HEADER_SIZE)
        magic, _ver, nrm_count = struct.unpack(_NRM_HEADER_FMT, header)
        if magic != _NRM_MAGIC:
            raise ValueError(f"{self._paths['nrm']} no es un archivo de normas")
        raw = self._read_blocks(self._nrm, 4 * nrm_count)
        self.norms = list(struct.unpack(f"<{nrm_count}f", raw)) \
            if nrm_count else []

    def _write_dic(self) -> None:
        self._dic.seek(0)
        self._dic.write(struct.pack(
            _DIC_HEADER_FMT, _DIC_MAGIC, 1, len(self.vocab), self.live_docs))
        for term in sorted(self.vocab):
            df, offset, length = self.vocab[term]
            raw = term.encode("utf-8")[:_MAX_TERM]
            self._dic.write(struct.pack(
                _DIC_ENTRY_FMT, raw.ljust(_MAX_TERM, b"\x00"),
                df, offset, length))
        self._dic.truncate()
        self._dic.flush()

    def _write_docs(self) -> None:
        self._docs.seek(0)
        self._docs.write(struct.pack(
            _DOCS_HEADER_FMT, _DOCS_MAGIC, 1, len(self.docs)))
        for page_id, slot, alive in self.docs:
            self._docs.write(struct.pack(
                _DOCS_ENTRY_FMT, page_id, slot, alive, 0))
        self._docs.truncate()
        self._docs.flush()

    def _write_norms(self) -> None:
        self._nrm.seek(0)
        self._nrm.write(struct.pack(
            _NRM_HEADER_FMT, _NRM_MAGIC, 1, len(self.norms)))
        if self.norms:
            self._nrm.write(struct.pack(f"<{len(self.norms)}f", *self.norms))
        self._nrm.truncate()
        self._nrm.flush()

    # ------------------------------------------------------------------
    # Listas de posteo en disco
    # ------------------------------------------------------------------
    def _read_postings(self, offset: int, length: int) -> list[tuple[int, int]]:
        """Lee una lista de posteo con un único seek+read (contado)."""
        self._inv.seek(offset)
        raw = self._inv.read(length * _POSTING_SIZE)
        return [struct.unpack_from(_POSTING_FMT, raw, i * _POSTING_SIZE)
                for i in range(length)]

    def _write_postings(self, postings: list[tuple[int, int]]) -> int:
        """Anexa una lista al final del ``.inv``; devuelve su offset."""
        self._inv.seek(0, os.SEEK_END)
        offset = self._inv.tell()
        data = b"".join(struct.pack(_POSTING_FMT, d, tf)
                        for d, tf in postings)
        self._inv.write(data)
        return offset

    def postings(self, term: str) -> list[tuple[int, int]]:
        """Lista de posteo viva de un término: ``[(doc_id, tf)]``.

        Fusiona la lista persistida con el delta aún no volcado y
        filtra los documentos eliminados (lápidas).
        """
        term = term.encode("utf-8")[:_MAX_TERM].decode("utf-8", "ignore")
        result: list[tuple[int, int]] = []
        entry = self.vocab.get(term)
        if entry is not None:
            _df, offset, length = entry
            result = [(d, tf) for d, tf in self._read_postings(offset, length)
                      if self.docs[d][2]]
        if term in self._delta:
            pending = [(d, tf) for d, tf in self._delta[term]
                       if self.docs[d][2]]
            result = _union_tf(result, sorted(pending))
        return result

    def df(self, term: str) -> int:
        """Frecuencia de documento viva (len de la lista filtrada)."""
        return len(self.postings(term))

    # ------------------------------------------------------------------
    # Construcción y mantenimiento
    # ------------------------------------------------------------------
    def add_document(self, rid: tuple[int, int], terms: list[str]) -> int:
        """Registra un documento nuevo (delta en memoria) y da su doc_id."""
        doc_id = len(self.docs)
        self.docs.append([rid[0], rid[1], 1])
        self.rid_to_doc[rid] = doc_id
        self.norms.append(0.0)
        self.live_docs += 1
        counts: dict[str, int] = {}
        for term in terms:
            term = term.encode("utf-8")[:_MAX_TERM].decode("utf-8", "ignore")
            counts[term] = counts.get(term, 0) + 1
        for term, tf in counts.items():
            self._delta.setdefault(term, []).append((doc_id, tf))
        self._dirty = True
        return doc_id

    def delete_document(self, rid: tuple[int, int]) -> bool:
        """Marca el documento del RID como eliminado (lápida)."""
        doc_id = self.rid_to_doc.pop(rid, None)
        if doc_id is None:
            return False
        self.docs[doc_id][2] = 0
        self.live_docs -= 1
        self._dirty = True
        return True

    def flush(self) -> None:
        """Fusiona el delta con las listas en disco y persiste todo.

        Cada término tocado reescribe su lista completa al final del
        ``.inv`` (espacio viejo se recupera en una compactación futura).
        Tras la fusión se recalculan las normas L2 de todos los
        documentos vivos con el idf vigente, en un solo pase sobre las
        listas.
        """
        if not self._dirty:
            return
        for term in sorted(self._delta):
            merged = self.postings(term)  # vieja + delta, sin lápidas
            offset = self._write_postings(merged)
            self.vocab[term] = [len(merged), offset, len(merged)]
        self._delta.clear()
        self._inv.flush()
        self._recompute_norms()
        self._write_dic()
        self._write_docs()
        self._write_norms()
        self._dirty = False

    def _recompute_norms(self) -> None:
        """Un pase sobre todas las listas: ||d||2 = sqrt(sum w^2)."""
        n = self.live_docs
        if n == 0:
            self.norms = [0.0] * len(self.docs)
            return
        acc = [0.0] * len(self.docs)
        for term, (df, offset, length) in self.vocab.items():
            if df == 0:
                continue
            idf = math.log10(n / df)
            for doc_id, tf in self._read_postings(offset, length):
                if self.docs[doc_id][2]:
                    w = (1.0 + math.log10(tf)) * idf
                    acc[doc_id] += w * w
        self.norms = [math.sqrt(v) for v in acc]

    def close(self) -> None:
        if self._dirty:
            self.flush()
        for f in (self._dic, self._inv, self._docs, self._nrm):
            f.close()

    # ------------------------------------------------------------------
    # Recuperación booleana (mezcla con dos punteros)
    # ------------------------------------------------------------------
    def search_boolean(self, ast: dict, preprocess) -> list[int]:
        """Evalúa el AST booleano del MATCH; devuelve doc_ids ordenados."""
        if "term" in ast:
            terms = preprocess.terms(ast["term"])
            if not terms:  # stopword pura: no aporta
                return []
            lists = [[d for d, _tf in self.postings(t)] for t in terms]
            result = lists[0]
            for other in lists[1:]:
                result = _intersect(result, other)
            return result
        if "and" in ast:
            lists = [self.search_boolean(e, preprocess) for e in ast["and"]]
            result = lists[0]
            for other in lists[1:]:
                result = _intersect(result, other)
            return result
        if "or" in ast:
            result: list[int] = []
            for e in ast["or"]:
                result = _union(result, self.search_boolean(e, preprocess))
            return result
        if "not" in ast:
            universe = [d for d in range(len(self.docs)) if self.docs[d][2]]
            return _difference(
                universe, self.search_boolean(ast["not"], preprocess))
        raise ValueError(f"AST booleano desconocido: {ast}")

    # ------------------------------------------------------------------
    # Ranking TF-IDF con similitud de coseno
    # ------------------------------------------------------------------
    def cosine_topk(self, query_terms: list[str], k: int) -> list[tuple[int, float]]:
        """CosineScore(q): Top-K de ``(doc_id, score)`` por similitud coseno.

        Acumula los productos ``w_q,t * w_d,t`` solo sobre las listas de
        los términos de la consulta y normaliza por ``||d||2 * ||q||2``
        (normas de documento leídas del ``.nrm`` cargado en memoria).
        """
        if self._dirty:
            self.flush()
        n = self.live_docs
        if n == 0 or k <= 0:
            return []
        counts: dict[str, int] = {}
        for term in query_terms:
            counts[term] = counts.get(term, 0) + 1

        scores: dict[int, float] = {}
        q_norm_sq = 0.0
        for term, tf_q in counts.items():
            post = self.postings(term)
            df = len(post)
            if df == 0:
                continue
            idf = math.log10(n / df)
            w_q = (1.0 + math.log10(tf_q)) * idf
            q_norm_sq += w_q * w_q
            for doc_id, tf_d in post:
                w_d = (1.0 + math.log10(tf_d)) * idf
                scores[doc_id] = scores.get(doc_id, 0.0) + w_q * w_d

        q_norm = math.sqrt(q_norm_sq)
        if q_norm == 0.0:
            return []
        ranked = [
            (doc_id, total / (self.norms[doc_id] * q_norm))
            for doc_id, total in scores.items()
            if self.norms[doc_id] > 0.0
        ]
        return heapq.nlargest(k, ranked, key=lambda t: t[1])


# ----------------------------------------------------------------------
# Mezcla de listas ordenadas con dos punteros
# ----------------------------------------------------------------------
def _intersect(a: list[int], b: list[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            out.append(a[i])
            i += 1
            j += 1
        elif a[i] < b[j]:
            i += 1
        else:
            j += 1
    return out


def _union(a: list[int], b: list[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            out.append(a[i])
            i += 1
            j += 1
        elif a[i] < b[j]:
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def _difference(a: list[int], b: list[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif a[i] < b[j]:
            out.append(a[i])
            i += 1
        else:
            j += 1
    out.extend(a[i:])
    return out


def _union_tf(a: list[tuple[int, int]],
              b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Mezcla dos listas ``(doc_id, tf)``; en colisión gana la de ``b``
    (el delta es más reciente)."""
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][0] == b[j][0]:
            out.append(b[j])
            i += 1
            j += 1
        elif a[i][0] < b[j][0]:
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out
