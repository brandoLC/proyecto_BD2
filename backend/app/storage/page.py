"""Página ranurada (slotted page) de 4 KB.

Layout físico de la página (4096 bytes)::

    [ header: slot_count (H), free_start (H) ]          4 bytes
    [ slot array: (offset H, length H, alive B, res B) ] 6 bytes por slot
    ... espacio libre ...
    [ tuplas escritas desde el final hacia atrás ]

Los registros se escriben desde el final de la página hacia el inicio.
``free_start`` marca el inicio del área de datos (crece hacia atrás).
Eliminar un registro solo marca su slot como muerto: el ``slot_id``
permanece estable, como exige el modelo RID = (page_id, slot_id).
"""

from __future__ import annotations

import struct

PAGE_SIZE = 4096

HEADER_FMT = "<HH"  # slot_count, free_start
HEADER_SIZE = struct.calcsize(HEADER_FMT)

SLOT_FMT = "<HHBB"  # offset, length, alive, reservado
SLOT_SIZE = struct.calcsize(SLOT_FMT)


class PageFullError(Exception):
    """La página no tiene espacio suficiente para el registro."""


class SlottedPage:
    """Página ranurada de tamaño fijo ``PAGE_SIZE``.

    Métodos principales: ``insert``, ``read``, ``delete`` (marca muerto),
    ``compact`` (recompacta eliminando huecos) y ``free_space``.
    """

    def __init__(self, data: bytes | None = None) -> None:
        if data is None:
            self.buffer = bytearray(PAGE_SIZE)
            self.slots: list[list[int]] = []  # [offset, length, alive]
            self.free_start = PAGE_SIZE
        else:
            if len(data) != PAGE_SIZE:
                raise ValueError(f"La página debe tener {PAGE_SIZE} bytes")
            self.buffer = bytearray(data)
            slot_count, self.free_start = struct.unpack_from(HEADER_FMT, self.buffer, 0)
            self.slots = []
            for i in range(slot_count):
                off, length, alive, _ = struct.unpack_from(
                    SLOT_FMT, self.buffer, HEADER_SIZE + i * SLOT_SIZE
                )
                self.slots.append([off, length, alive])

    # ------------------------------------------------------------------
    # Serialización
    # ------------------------------------------------------------------
    def to_bytes(self) -> bytes:
        """Serializa la página completa a un buffer de 4096 bytes."""
        struct.pack_into(HEADER_FMT, self.buffer, 0, len(self.slots), self.free_start)
        for i, (off, length, alive) in enumerate(self.slots):
            struct.pack_into(
                SLOT_FMT, self.buffer, HEADER_SIZE + i * SLOT_SIZE, off, length, alive, 0
            )
        return bytes(self.buffer)

    @classmethod
    def from_bytes(cls, data: bytes) -> "SlottedPage":
        """Reconstruye una página desde su buffer serializado."""
        return cls(data)

    # ------------------------------------------------------------------
    # Operaciones
    # ------------------------------------------------------------------
    def free_space(self) -> int:
        """Espacio libre contiguo entre el slot array y el área de datos."""
        return self.free_start - (HEADER_SIZE + SLOT_SIZE * len(self.slots))

    def insert(self, record: bytes) -> int:
        """Inserta un registro y devuelve el ``slot_id`` asignado.

        Reutiliza el primer slot muerto disponible; si no hay, agrega un
        slot nuevo. Si el espacio contiguo no alcanza, intenta compactar
        antes de rendirse con ``PageFullError``.
        """
        reusable = None
        for i, (_, _, alive) in enumerate(self.slots):
            if not alive:
                reusable = i
                break

        needed = len(record) + (0 if reusable is not None else SLOT_SIZE)
        if needed > self.free_space():
            self.compact()
        if needed > self.free_space():
            raise PageFullError(
                f"registro de {len(record)} bytes no cabe en la página"
            )

        self.free_start -= len(record)
        self.buffer[self.free_start : self.free_start + len(record)] = record

        if reusable is not None:
            self.slots[reusable] = [self.free_start, len(record), 1]
            return reusable
        self.slots.append([self.free_start, len(record), 1])
        return len(self.slots) - 1

    def read(self, slot_id: int) -> bytes:
        """Lee el registro de un slot; falla si el slot está muerto."""
        if not (0 <= slot_id < len(self.slots)):
            raise KeyError(f"slot inválido: {slot_id}")
        off, length, alive = self.slots[slot_id]
        if not alive:
            raise KeyError(f"slot {slot_id} está eliminado")
        return bytes(self.buffer[off : off + length])

    def delete(self, slot_id: int) -> None:
        """Marca el slot como muerto. El ``slot_id`` permanece estable."""
        if not (0 <= slot_id < len(self.slots)):
            raise KeyError(f"slot inválido: {slot_id}")
        self.slots[slot_id][2] = 0

    def is_alive(self, slot_id: int) -> bool:
        return 0 <= slot_id < len(self.slots) and bool(self.slots[slot_id][2])

    def alive_count(self) -> int:
        return sum(1 for _, _, alive in self.slots if alive)

    def compact(self) -> None:
        """Reescribe los registros vivos eliminando la fragmentación.

        Los registros vivos se copian al final de la página en el orden de
        sus slots, actualizando los offsets. Los slots muertos se conservan
        (marcados como muertos) para mantener estable el ``slot_id``.
        """
        live: list[tuple[int, bytes]] = []
        for i, (off, length, alive) in enumerate(self.slots):
            if alive:
                live.append((i, bytes(self.buffer[off : off + length])))

        new_start = PAGE_SIZE
        for i, data in live:
            new_start -= len(data)
            self.buffer[new_start : new_start + len(data)] = data
            self.slots[i][0] = new_start
        self.free_start = new_start

    def iter_alive(self):
        """Itera ``(slot_id, record_bytes)`` de los registros vivos."""
        for i, (off, length, alive) in enumerate(self.slots):
            if alive:
                yield i, bytes(self.buffer[off : off + length])
