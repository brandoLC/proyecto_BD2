"""Sequential file: área principal ordenada + área de overflow encadenada.

Dos archivos por tabla:

- ``<base>.seq`` — área principal: página 0 = cabecera (magic, page_count,
  row_count, key_size, punteros head/tail de la cadena); páginas 1..N con
  los registros ordenados físicamente por clave (admite búsqueda binaria
  por páginas).
- ``<base>.ovf`` — área de overflow: página 0 = cabecera con una free list
  simple de slots muertos reutilizables; páginas 1..M con los registros
  insertados que no caben en la principal.

Formato de registro en ambas áreas::

    [ key_bytes (key_size fijo) ][ next_page: I ][ next_slot: H ][ payload ]

``next`` es un puntero binario al siguiente registro en orden lógico; en la
principal apunta al siguiente registro físico salvo que la cadena se desvíe
al overflow. ``next = (0, 0)`` marca el fin de la cadena (la página 0 es
siempre cabecera, nunca un registro).

Inserción: búsqueda binaria por páginas en la principal (comparando la
primera clave de cada página) para ubicar al predecesor, caminata corta por
la cadena, inserción en el ``.ovf`` y reenlace (``new.next = pred.next;
pred.next = new``). Si el archivo está vacío se inserta directo en la
principal. Eliminación: reenlace del predecesor + slot marcado muerto; el
espacio de la principal se recupera en ``reorganize``, el del overflow se
reutiliza vía la free list.

RID uniforme ``(page_id, slot_id)``: a los registros del overflow se les
suma ``OVF_BASE`` al ``page_id`` para distinguir el área (ver
``is_overflow_rid``).

Invariante: las páginas de la principal jamás se compactan teniendo slots
muertos (los registros muertos conservan sus bytes y su clave, lo que
mantiene válida la búsqueda binaria). Cuando el último registro vivo se
elimina, ambas áreas se truncan a su cabecera.
"""

from __future__ import annotations

import os
import struct
from typing import Iterator

from .disk_counter import CountedFile, DiskCounter
from .page import PAGE_SIZE, PageFullError, SlottedPage

SEQ_MAGIC = b"SEQ1"
# magic, page_count, row_count, key_size, head(page, slot), tail(page, slot)
SEQ_HEADER_FMT = "<4sIIHIHIH"
SEQ_HEADER_SIZE = struct.calcsize(SEQ_HEADER_FMT)

OVF_MAGIC = b"OVF1"
# magic, page_count, row_count, free_count, dropped_free
OVF_HEADER_FMT = "<4sIIHI"
OVF_HEADER_SIZE = struct.calcsize(OVF_HEADER_FMT)

FREE_ENTRY_FMT = "<IH"  # page_id, slot_id
FREE_ENTRY_SIZE = struct.calcsize(FREE_ENTRY_FMT)
MAX_FREE = (PAGE_SIZE - OVF_HEADER_SIZE) // FREE_ENTRY_SIZE

NEXT_FMT = "<IH"  # next_page, next_slot
NEXT_SIZE = struct.calcsize(NEXT_FMT)

RID = tuple[int, int]
END: RID = (0, 0)  # fin de cadena / cadena vacía (página 0 = cabecera)

# Base sumada al page_id de los RIDs del área de overflow.
OVF_BASE = 1 << 20

_PAGE_HEADER = 4  # cabecera de SlottedPage (slot_count, free_start)
_SLOT_SIZE = 6    # entrada del slot array de SlottedPage


def is_overflow_rid(rid: RID) -> bool:
    """True si el RID pertenece al área de overflow."""
    return rid[0] >= OVF_BASE


class SequentialFile:
    """Sequential file con búsqueda binaria, overflow encadenado y
    reorganización periódica con fill factor.

    ``encode_key``/``decode_key`` convierten valores Python a/de bytes de
    longitud fija ``key_size`` (ver ``storage.record.encode_key``).
    ``payload`` son los bytes del registro completo ya serializado.

    Sigue el patrón ``defer_header``/``defer_flush`` de ``HeapFile`` para
    carga masiva: con ``defer_header`` la cabecera se vuelca al final
    (``flush_header``/``close``); con ``defer_flush`` las páginas
    modificadas se mantienen en memoria y se escriben una sola vez
    (``flush_pages``/``close``). La caché de páginas es read-through y
    siempre está activa; todas las mutaciones pasan por ``_write_*_page``,
    que la mantiene sincronizada.
    """

    def __init__(self, seq_path: str, ovf_path: str, key_size: int,
                 encode_key, decode_key, create: bool = False,
                 counter: DiskCounter | None = None) -> None:
        self.seq_path = seq_path
        self.ovf_path = ovf_path
        self.key_size = key_size
        self.encode_key = encode_key
        self.decode_key = decode_key
        self.defer_header = False
        self._seq_header_dirty = False
        self._ovf_header_dirty = False
        self.defer_flush = False
        self._seq_cache: dict[int, SlottedPage] = {}
        self._seq_dirty: set[int] = set()
        self._ovf_cache: dict[int, SlottedPage] = {}
        self._ovf_dirty: set[int] = set()
        self._cache_max = 4096
        self._counter = counter

        if create or not os.path.exists(seq_path):
            self.page_count = 1  # página 0 = cabecera
            self.row_count = 0   # registros vivos totales (ambas áreas)
            self.head: RID = END
            self.tail: RID = END
            self._seq = open(seq_path, "w+b")
            if counter is not None:
                self._seq = CountedFile(self._seq, counter)
            self._write_seq_header()
        else:
            self._seq = open(seq_path, "r+b")
            if counter is not None:
                self._seq = CountedFile(self._seq, counter)
            self._read_seq_header()
            if key_size != self.key_size:
                raise ValueError("key_size incompatible con el archivo")

        if create or not os.path.exists(ovf_path):
            self.ovf_page_count = 1
            self.ovf_row_count = 0
            self.free_list: list[RID] = []
            self.dropped_free = 0
            self._ovf = open(ovf_path, "w+b")
            if counter is not None:
                self._ovf = CountedFile(self._ovf, counter)
            self._write_ovf_header()
        else:
            self._ovf = open(ovf_path, "r+b")
            if counter is not None:
                self._ovf = CountedFile(self._ovf, counter)
            self._read_ovf_header()

    # ------------------------------------------------------------------
    # Cabeceras
    # ------------------------------------------------------------------
    def _read_seq_header(self) -> None:
        self._seq.seek(0)
        data = self._seq.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != SEQ_MAGIC:
            raise ValueError(f"{self.seq_path} no es un sequential file válido")
        (_, self.page_count, self.row_count, self.key_size,
         hp, hs, tp, ts) = struct.unpack_from(SEQ_HEADER_FMT, data, 0)
        self.head = (hp, hs)
        self.tail = (tp, ts)

    def _write_seq_header(self) -> None:
        if self.defer_header:
            self._seq_header_dirty = True
            return
        self._flush_seq_header()

    def _flush_seq_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            SEQ_HEADER_FMT, buf, 0, SEQ_MAGIC, self.page_count,
            self.row_count, self.key_size,
            self.head[0], self.head[1], self.tail[0], self.tail[1],
        )
        self._seq.seek(0)
        self._seq.write(buf)
        self._seq.flush()
        self._seq_header_dirty = False

    def _read_ovf_header(self) -> None:
        self._ovf.seek(0)
        data = self._ovf.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != OVF_MAGIC:
            raise ValueError(f"{self.ovf_path} no es un overflow file válido")
        (_, self.ovf_page_count, self.ovf_row_count, free_count,
         self.dropped_free) = struct.unpack_from(OVF_HEADER_FMT, data, 0)
        self.free_list = []
        for i in range(free_count):
            page_id, slot_id = struct.unpack_from(
                FREE_ENTRY_FMT, data, OVF_HEADER_SIZE + i * FREE_ENTRY_SIZE
            )
            self.free_list.append((page_id, slot_id))

    def _write_ovf_header(self) -> None:
        if self.defer_header:
            self._ovf_header_dirty = True
            return
        self._flush_ovf_header()

    def _flush_ovf_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            OVF_HEADER_FMT, buf, 0, OVF_MAGIC, self.ovf_page_count,
            self.ovf_row_count, len(self.free_list), self.dropped_free,
        )
        for i, (page_id, slot_id) in enumerate(self.free_list):
            struct.pack_into(
                FREE_ENTRY_FMT, buf, OVF_HEADER_SIZE + i * FREE_ENTRY_SIZE,
                page_id, slot_id,
            )
        self._ovf.seek(0)
        self._ovf.write(buf)
        self._ovf.flush()
        self._ovf_header_dirty = False

    def flush_header(self) -> None:
        """Vuelca las cabeceras pendientes (carga masiva)."""
        if self._seq_header_dirty:
            self._flush_seq_header()
        if self._ovf_header_dirty:
            self._flush_ovf_header()

    # ------------------------------------------------------------------
    # Páginas (caché read-through; write-back solo con defer_flush)
    # ------------------------------------------------------------------
    def _read_seq_page(self, page_id: int) -> SlottedPage:
        page = self._seq_cache.get(page_id)
        if page is None:
            if len(self._seq_cache) >= self._cache_max:
                self.flush_pages()
            self._seq.seek(page_id * PAGE_SIZE)
            page = SlottedPage.from_bytes(self._seq.read(PAGE_SIZE))
            self._seq_cache[page_id] = page
        return page

    def _read_ovf_page(self, page_id: int) -> SlottedPage:
        page = self._ovf_cache.get(page_id)
        if page is None:
            if len(self._ovf_cache) >= self._cache_max:
                self.flush_pages()
            self._ovf.seek(page_id * PAGE_SIZE)
            page = SlottedPage.from_bytes(self._ovf.read(PAGE_SIZE))
            self._ovf_cache[page_id] = page
        return page

    def _write_seq_page(self, page_id: int, page: SlottedPage) -> None:
        if self.defer_flush:
            self._seq_cache[page_id] = page
            self._seq_dirty.add(page_id)
            return
        self._seq.seek(page_id * PAGE_SIZE)
        self._seq.write(page.to_bytes())
        self._seq.flush()
        self._seq_cache[page_id] = page

    def _write_ovf_page(self, page_id: int, page: SlottedPage) -> None:
        if self.defer_flush:
            self._ovf_cache[page_id] = page
            self._ovf_dirty.add(page_id)
            return
        self._ovf.seek(page_id * PAGE_SIZE)
        self._ovf.write(page.to_bytes())
        self._ovf.flush()
        self._ovf_cache[page_id] = page

    def flush_pages(self) -> None:
        """Vuelca las páginas modificadas de ambas áreas (carga masiva)."""
        for page_id in sorted(self._seq_dirty):
            self._seq.seek(page_id * PAGE_SIZE)
            self._seq.write(self._seq_cache[page_id].to_bytes())
        for page_id in sorted(self._ovf_dirty):
            self._ovf.seek(page_id * PAGE_SIZE)
            self._ovf.write(self._ovf_cache[page_id].to_bytes())
        if self._seq_dirty or self._ovf_dirty:
            self._seq.flush()
            self._ovf.flush()
        self._seq_dirty.clear()
        self._ovf_dirty.clear()
        self._seq_cache.clear()
        self._ovf_cache.clear()

    def _page_of(self, rid: RID) -> SlottedPage:
        if is_overflow_rid(rid):
            return self._read_ovf_page(rid[0] - OVF_BASE)
        return self._read_seq_page(rid[0])

    def _write_page_of(self, rid: RID, page: SlottedPage) -> None:
        if is_overflow_rid(rid):
            self._write_ovf_page(rid[0] - OVF_BASE, page)
        else:
            self._write_seq_page(rid[0], page)

    # ------------------------------------------------------------------
    # Registros
    # ------------------------------------------------------------------
    def _pack_record(self, key, nxt: RID, payload: bytes) -> bytes:
        return (self.encode_key(key)
                + struct.pack(NEXT_FMT, nxt[0], nxt[1])
                + payload)

    def _unpack_record(self, rec: bytes):
        key = self.decode_key(rec[: self.key_size])
        nxt = struct.unpack_from(NEXT_FMT, rec, self.key_size)
        return key, nxt, rec[self.key_size + NEXT_SIZE:]

    def _read_record_at(self, rid: RID):
        """``(key, next, payload, alive)``; lee incluso slots muertos.

        Los registros muertos de la principal conservan sus bytes (ver el
        invariante del docstring del módulo), así que su clave sigue siendo
        válida para la búsqueda binaria y la caminata hacia atrás.
        """
        page = self._page_of(rid)
        off, length, alive = page.slots[rid[1]]
        rec = bytes(page.buffer[off : off + length])
        key, nxt, payload = self._unpack_record(rec)
        return key, nxt, payload, bool(alive)

    def _set_next(self, rid: RID, nxt: RID) -> None:
        """Reescribe in situ el puntero ``next`` de un registro vivo."""
        page = self._page_of(rid)
        off, _, _ = page.slots[rid[1]]
        struct.pack_into(NEXT_FMT, page.buffer, off + self.key_size, *nxt)
        self._write_page_of(rid, page)

    def _page_first_key(self, page: SlottedPage):
        """Clave del primer registro físico de una página de la principal."""
        off, _, _ = page.slots[0]
        return self.decode_key(bytes(page.buffer[off : off + self.key_size]))

    # ------------------------------------------------------------------
    # Localización del punto de partida de la caminata
    # ------------------------------------------------------------------
    def _lower_bound_page(self, key) -> int:
        """Última página de la principal con primera clave <= key; 0 si no hay."""
        lo, hi, ans = 1, self.page_count - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._page_first_key(self._read_seq_page(mid)) <= key:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    def _start(self, key, strict: bool) -> RID | None:
        """Último registro VIVO de la principal con clave < key (``strict``)
        o <= key; None si no existe (la caminata empieza en ``head``)."""
        for pid in range(self._lower_bound_page(key), 0, -1):
            page = self._read_seq_page(pid)
            for sid in range(len(page.slots) - 1, -1, -1):
                off, _, alive = page.slots[sid]
                if not alive:
                    continue
                k = self.decode_key(bytes(page.buffer[off : off + self.key_size]))
                in_range = k < key if strict else k <= key
                if in_range:
                    return (pid, sid)
        return None

    def _walk_from(self, anchor: RID | None) -> tuple[RID | None, RID]:
        """``(prev, cur)`` iniciales de la caminata: desde ``anchor`` si hay
        punto de partida en la principal, si no desde ``head``."""
        return None, anchor if anchor is not None else self.head

    # ------------------------------------------------------------------
    # Inserción
    # ------------------------------------------------------------------
    def insert(self, key, payload: bytes) -> RID:
        """Inserta ``(key, payload)`` manteniendo el orden lógico.

        Con el archivo vacío va directo a la principal; en caso contrario
        se inserta en el overflow reenlazando al predecesor. Claves
        duplicadas se aceptan: van después de las existentes. Devuelve el
        RID (con ``OVF_BASE`` si quedó en el overflow).
        """
        if self.head == END:  # archivo vacío: directo a la principal
            rid = self._append_main_record(key, payload)
            self.head = self.tail = rid
            self.row_count += 1
            self._write_seq_header()
            return rid

        # Camino rápido: inserción al final (cargas ordenadas).
        pred: RID | None = None
        tkey = self._read_record_at(self.tail)[0]
        if key >= tkey:
            pred = self.tail
        else:
            anchor = self._start(key, strict=False)
            prev, cur = self._walk_from(anchor)
            while cur != END:
                k, nxt, _, _ = self._read_record_at(cur)
                if k > key:
                    break
                prev, cur = cur, nxt
            pred = prev

        if pred is None:  # nuevo primer registro de la cadena
            rid = self._ovf_insert(key, self.head, payload)
            self.head = rid
        else:
            _, nxt, _, _ = self._read_record_at(pred)
            rid = self._ovf_insert(key, nxt, payload)
            self._set_next(pred, rid)
            if pred == self.tail:
                self.tail = rid
        self.row_count += 1
        self._write_seq_header()
        return rid

    def _append_main_record(self, key, payload: bytes) -> RID:
        """Agrega el primer registro del archivo en una página nueva."""
        rec = self._pack_record(key, END, payload)
        page = SlottedPage()
        try:
            sid = page.insert(rec)
        except PageFullError as exc:
            raise ValueError(
                f"registro de {len(rec)} bytes no cabe en una página de 4 KB"
            ) from exc
        page_id = self.page_count
        self._write_seq_page(page_id, page)
        self.page_count += 1
        return (page_id, sid)

    def _ovf_insert(self, key, nxt: RID, payload: bytes) -> RID:
        """Inserta en el overflow: free list, última página o página nueva."""
        rec = self._pack_record(key, nxt, payload)
        # Free list simple: un intento con el último slot libre conocido;
        # si el registro no cabe ahí se devuelve y se sigue al final.
        if self.free_list:
            frid = self.free_list.pop()
            page = self._read_ovf_page(frid[0])
            try:
                sid = page.insert(rec)
                self._write_ovf_page(frid[0], page)
                self.ovf_row_count += 1
                self._write_ovf_header()
                return (frid[0] + OVF_BASE, sid)
            except PageFullError:
                self.free_list.append(frid)

        if self.ovf_page_count > 1:
            last = self.ovf_page_count - 1
            page = self._read_ovf_page(last)
            try:
                sid = page.insert(rec)
                self._write_ovf_page(last, page)
                self.ovf_row_count += 1
                self._write_ovf_header()
                return (last + OVF_BASE, sid)
            except PageFullError:
                pass

        page = SlottedPage()
        try:
            sid = page.insert(rec)
        except PageFullError as exc:
            raise ValueError(
                f"registro de {len(rec)} bytes no cabe en una página de 4 KB"
            ) from exc
        page_id = self.ovf_page_count
        self._write_ovf_page(page_id, page)
        self.ovf_page_count += 1
        self.ovf_row_count += 1
        self._write_ovf_header()
        return (page_id + OVF_BASE, sid)

    # ------------------------------------------------------------------
    # Búsquedas
    # ------------------------------------------------------------------
    def search(self, key) -> tuple[RID, bytes] | None:
        """Busca por clave exacta: binaria en la principal + cadena.

        Devuelve ``(RID, payload)`` de la primera ocurrencia viva o None.
        """
        if self.head == END:
            return None
        if key > self._read_record_at(self.tail)[0]:
            return None
        anchor = self._start(key, strict=True)
        _, cur = self._walk_from(anchor)
        while cur != END:
            k, nxt, payload, _ = self._read_record_at(cur)
            if k == key:
                return cur, payload
            if k > key:
                return None
            cur = nxt
        return None

    def range_search(self, lo=None, hi=None,
                     lo_inc: bool = True, hi_inc: bool = True
                     ) -> list[tuple[RID, object, bytes]]:
        """``(RID, key, payload)`` dentro del rango, en orden.

        Por defecto ``lo <= key <= hi`` (ambos inclusivos). ``lo``/``hi``
        en None dejan el lado abierto; ``lo_inc``/``hi_inc`` permiten
        extremos exclusivos (para mapear ``<``/``>`` del SQL).
        """
        out: list[tuple[RID, object, bytes]] = []
        if self.head == END:
            return out
        if lo is not None and hi is not None:
            if lo > hi or (lo == hi and not (lo_inc and hi_inc)):
                return out
        anchor = self._start(lo, strict=True) if lo is not None else None
        _, cur = self._walk_from(anchor)
        while cur != END:
            k, nxt, payload, _ = self._read_record_at(cur)
            if hi is not None and (k > hi or (k == hi and not hi_inc)):
                break
            if lo is None or k > lo or (k == lo and lo_inc):
                out.append((cur, k, payload))
            cur = nxt
        return out

    def scan(self) -> Iterator[tuple[RID, object, bytes]]:
        """Itera ``(RID, key, payload)`` vivos en orden, siguiendo la cadena."""
        cur = self.head
        while cur != END:
            k, nxt, payload, _ = self._read_record_at(cur)
            yield cur, k, payload
            cur = nxt

    def read(self, rid: RID) -> tuple[object, bytes]:
        """``(key, payload)`` del registro del RID; falla si está muerto."""
        key, _, payload, alive = self._read_record_at(rid)
        if not alive:
            raise KeyError(f"RID {rid} está eliminado")
        return key, payload

    # ------------------------------------------------------------------
    # Eliminación
    # ------------------------------------------------------------------
    def delete(self, key) -> bool:
        """Elimina la primera ocurrencia viva de ``key``.

        Reenlaza al predecesor (o ``head``) y marca el slot muerto; los
        slots del overflow pasan a la free list. Devuelve False si la clave
        no existe.
        """
        if self.head == END:
            return False
        if key > self._read_record_at(self.tail)[0]:
            return False
        anchor = self._start(key, strict=True)
        prev, cur = self._walk_from(anchor)
        while cur != END:
            k, nxt, _, _ = self._read_record_at(cur)
            if k >= key:
                break
            prev, cur = cur, nxt
        if cur == END:
            return False
        k, nxt, _, _ = self._read_record_at(cur)
        if k != key:
            return False

        if prev is None:
            self.head = nxt
        else:
            self._set_next(prev, nxt)
        if cur == self.tail:
            self.tail = prev if prev is not None else END

        page = self._page_of(cur)
        page.delete(cur[1])
        self._write_page_of(cur, page)
        self.row_count -= 1
        if is_overflow_rid(cur):
            self.ovf_row_count -= 1
            ovid = (cur[0] - OVF_BASE, cur[1])
            if len(self.free_list) < MAX_FREE:
                self.free_list.append(ovid)
            else:
                self.dropped_free += 1
            self._write_ovf_header()

        if self.row_count == 0:
            # Archivo lógicamente vacío: truncar ambas áreas a su cabecera
            # (mantiene el invariante de la principal ordenada).
            self._reset_areas()
        self._write_seq_header()
        return True

    def _reset_areas(self) -> None:
        """Trunca la principal y el overflow a solo su cabecera."""
        self.page_count = 1
        self.head = self.tail = END
        self._seq.seek(0)
        self._seq.truncate(0)
        self._flush_seq_header()
        self.ovf_page_count = 1
        self.ovf_row_count = 0
        self.free_list = []
        self.dropped_free = 0
        self._ovf.seek(0)
        self._ovf.truncate(0)
        self._flush_ovf_header()
        self._seq_cache.clear()
        self._seq_dirty.clear()
        self._ovf_cache.clear()
        self._ovf_dirty.clear()

    # ------------------------------------------------------------------
    # Reorganización
    # ------------------------------------------------------------------
    def reorganize(self, fill_factor: float = 0.75) -> dict:
        """Fusiona principal + overflow y reescribe la principal limpia.

        Recorre la cadena completa (vivos de ambas áreas en orden),
        reescribe un ``.seq`` nuevo con páginas llenas al ``fill_factor``
        (entre 0.7 y 0.8), donde ``next`` vuelve a ser el siguiente físico,
        y trunca el ``.ovf`` a su cabecera. Reemplazo atómico del ``.seq``
        (archivo temporal + ``os.replace``). Devuelve estadísticas.
        """
        if not 0.7 <= fill_factor <= 0.8:
            raise ValueError("fill_factor debe estar entre 0.7 y 0.8")

        records = [(k, payload) for _, k, payload in self.scan()]

        # Muertos purgados: slots totales (ambas áreas) - vivos.
        total_slots = 0
        for pid in range(1, self.page_count):
            total_slots += len(self._read_seq_page(pid).slots)
        for pid in range(1, self.ovf_page_count):
            total_slots += len(self._read_ovf_page(pid).slots)
        dead_purged = total_slots - len(records)
        pages_before = self.page_count
        ovf_pages_before = self.ovf_page_count

        # Empaquetado greedy al fill factor sobre el espacio usable.
        target = int(PAGE_SIZE * fill_factor)
        packed: list[list[tuple[object, bytes]]] = []
        cur: list[tuple[object, bytes]] = []
        used = _PAGE_HEADER
        for k, payload in records:
            rec_len = self.key_size + NEXT_SIZE + len(payload)
            if _PAGE_HEADER + _SLOT_SIZE + rec_len > PAGE_SIZE:
                raise ValueError(
                    f"registro de {rec_len} bytes no cabe en una página de 4 KB"
                )
            if cur and used + _SLOT_SIZE + rec_len > target:
                packed.append(cur)
                cur, used = [], _PAGE_HEADER
            cur.append((k, payload))
            used += _SLOT_SIZE + rec_len
        if cur:
            packed.append(cur)

        # Escritura del .seq nuevo (tmp + os.replace).
        tmp_path = self.seq_path + ".tmp"
        n_pages = 1 + len(packed)
        head: RID = (1, 0) if packed else END
        tail: RID = (len(packed), len(packed[-1]) - 1) if packed else END
        with open(tmp_path, "w+b") as f:
            buf = bytearray(PAGE_SIZE)
            struct.pack_into(
                SEQ_HEADER_FMT, buf, 0, SEQ_MAGIC, n_pages, len(records),
                self.key_size, head[0], head[1], tail[0], tail[1],
            )
            f.seek(0)
            f.write(buf)
            for i, recs in enumerate(packed, start=1):
                page = SlottedPage()
                for j, (k, payload) in enumerate(recs):
                    if j + 1 < len(recs):
                        nxt = (i, j + 1)
                    elif i < len(packed):
                        nxt = (i + 1, 0)
                    else:
                        nxt = END
                    page.insert(self._pack_record(k, nxt, payload))
                f.seek(i * PAGE_SIZE)
                f.write(page.to_bytes())
            f.flush()

        self._seq.close()
        os.replace(tmp_path, self.seq_path)
        self._seq = open(self.seq_path, "r+b")
        if self._counter is not None:
            self._seq = CountedFile(self._seq, self._counter)

        self.page_count = n_pages
        self.row_count = len(records)
        self.head = head
        self.tail = tail
        self._seq_header_dirty = False

        # El overflow queda vacío (solo cabecera).
        self.ovf_page_count = 1
        self.ovf_row_count = 0
        self.free_list = []
        self.dropped_free = 0
        self._ovf.seek(0)
        self._ovf.truncate(0)
        self._flush_ovf_header()

        self._seq_cache.clear()
        self._seq_dirty.clear()
        self._ovf_cache.clear()
        self._ovf_dirty.clear()

        return {
            "pages_before": pages_before,
            "pages_after": self.page_count,
            "rows_moved": len(records),
            "dead_purged": dead_purged,
            "ovf_pages_before": ovf_pages_before,
            "ovf_pages_after": self.ovf_page_count,
        }

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def close(self) -> None:
        self.flush_pages()
        self.flush_header()
        self._seq.close()
        self._ovf.close()

    def __enter__(self) -> "SequentialFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
