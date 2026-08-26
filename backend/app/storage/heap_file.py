"""Heap file: archivo de páginas ranuradas de 4 KB con lista de libres.

Estructura del archivo:

- Página 0 (cabecera): magic, número de páginas, número de registros
  vivos y la lista de slots libres (RIDs de registros eliminados).
- Páginas 1..N: páginas ranuradas con los registros.

RID = (page_id, slot_id). La inserción primero reutiliza slots de la
lista de libres; si está vacía, intenta la última página y, en última
instancia, agrega una página nueva. Si la lista de libres se llenó y se
descartaron entradas, la inserción cae a un escaneo de páginas buscando
slots muertos antes de agregar página nueva.
"""

from __future__ import annotations

import os
import struct
from typing import Iterator

from .page import PAGE_SIZE, PageFullError, SlottedPage

MAGIC = b"HEAP"
HEADER_FMT = "<4sIIHI"  # magic, page_count, row_count, free_count, dropped_free
HEADER_SIZE = struct.calcsize(HEADER_FMT)

FREE_ENTRY_FMT = "<IH"  # page_id, slot_id
FREE_ENTRY_SIZE = struct.calcsize(FREE_ENTRY_FMT)

# Máximo de RIDs en la lista de libres que cabe en la página de cabecera.
MAX_FREE = (PAGE_SIZE - HEADER_SIZE) // FREE_ENTRY_SIZE

RID = tuple[int, int]


class HeapFile:
    """Archivo heap sobre páginas de 4 KB con lista de slots libres."""

    def __init__(self, path: str, create: bool = False) -> None:
        self.path = path
        if create or not os.path.exists(path):
            self.page_count = 1  # página 0 = cabecera
            self.row_count = 0
            self.free_list: list[RID] = []
            self.dropped_free = 0  # slots muertos que no cupieron en la lista
            self._file = open(path, "w+b")
            self._write_header()
        else:
            self._file = open(path, "r+b")
            self._read_header()

    # ------------------------------------------------------------------
    # Cabecera
    # ------------------------------------------------------------------
    def _read_header(self) -> None:
        self._file.seek(0)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE or data[:4] != MAGIC:
            raise ValueError(f"{self.path} no es un heap file válido")
        _, self.page_count, self.row_count, free_count, self.dropped_free = \
            struct.unpack_from(HEADER_FMT, data, 0)
        self.free_list = []
        for i in range(free_count):
            page_id, slot_id = struct.unpack_from(
                FREE_ENTRY_FMT, data, HEADER_SIZE + i * FREE_ENTRY_SIZE
            )
            self.free_list.append((page_id, slot_id))

    def _write_header(self) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into(
            HEADER_FMT, buf, 0, MAGIC, self.page_count, self.row_count,
            len(self.free_list), self.dropped_free,
        )
        for i, (page_id, slot_id) in enumerate(self.free_list):
            struct.pack_into(
                FREE_ENTRY_FMT, buf, HEADER_SIZE + i * FREE_ENTRY_SIZE,
                page_id, slot_id,
            )
        self._file.seek(0)
        self._file.write(buf)
        self._file.flush()

    # ------------------------------------------------------------------
    # Páginas
    # ------------------------------------------------------------------
    def _read_page(self, page_id: int) -> SlottedPage:
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        return SlottedPage.from_bytes(data)

    def _write_page(self, page_id: int, page: SlottedPage) -> None:
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(page.to_bytes())
        self._file.flush()

    def _append_page(self) -> int:
        page = SlottedPage()
        page_id = self.page_count
        self._write_page(page_id, page)
        self.page_count += 1
        return page_id

    # ------------------------------------------------------------------
    # Operaciones sobre registros (bytes ya serializados)
    # ------------------------------------------------------------------
    def insert(self, record: bytes) -> RID:
        """Inserta un registro y devuelve su RID.

        Orden de intentos: lista de libres -> escaneo de slots muertos ->
        última página -> página nueva.
        """
        # 1) reutilizar slots de la lista de libres
        while self.free_list:
            page_id, slot_id = self.free_list.pop()
            try:
                page = self._read_page(page_id)
                new_slot = page.insert(record)
                self._write_page(page_id, page)
                self.row_count += 1
                self._write_header()
                return (page_id, new_slot)
            except PageFullError:
                continue  # el slot muerto no acomoda este registro

        # 2) escaneo de respaldo: solo si la lista de libres desbordó
        #    (hay slots muertos sin registrar); si no, es O(páginas) en vano
        if self.dropped_free:
            rid = self._scan_dead_slot(record)
            if rid is not None:
                self.dropped_free -= 1
                return rid

        # 3) última página
        if self.page_count > 1:
            last = self.page_count - 1
            page = self._read_page(last)
            try:
                slot = page.insert(record)
                self._write_page(last, page)
                self.row_count += 1
                self._write_header()
                return (last, slot)
            except PageFullError:
                pass

        # 4) página nueva
        page_id = self._append_page()
        page = self._read_page(page_id)
        slot = page.insert(record)
        self._write_page(page_id, page)
        self.row_count += 1
        self._write_header()
        return (page_id, slot)

    def _scan_dead_slot(self, record: bytes) -> RID | None:
        for page_id in range(1, self.page_count):
            page = self._read_page(page_id)
            if any(not alive for _, _, alive in page.slots):
                try:
                    slot = page.insert(record)
                except PageFullError:
                    continue
                self._write_page(page_id, page)
                self.row_count += 1
                self._write_header()
                return (page_id, slot)
        return None

    def read(self, rid: RID) -> bytes:
        """Lee el registro de un RID; falla si está eliminado."""
        page_id, slot_id = rid
        return self._read_page(page_id).read(slot_id)

    def delete(self, rid: RID) -> None:
        """Marca el slot como muerto y lo empuja a la lista de libres."""
        page_id, slot_id = rid
        page = self._read_page(page_id)
        page.delete(slot_id)
        self._write_page(page_id, page)
        self.row_count -= 1
        if len(self.free_list) < MAX_FREE:
            self.free_list.append(rid)
        else:
            self.dropped_free += 1  # slot muerto sin registrar: hay que buscarlo luego
        self._write_header()

    def scan(self) -> Iterator[tuple[RID, bytes]]:
        """Escaneo completo: itera ``(rid, record_bytes)`` de vivos."""
        for page_id in range(1, self.page_count):
            page = self._read_page(page_id)
            for slot_id, record in page.iter_alive():
                yield (page_id, slot_id), record

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "HeapFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
