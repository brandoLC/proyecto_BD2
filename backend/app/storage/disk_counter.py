"""Contador de operaciones de I/O a disco.

Las páginas son de 4 KB y el motor siempre lee/escribe páginas completas,
así que una llamada ``read()``/``write()`` no vacía equivale a un bloque
físico. ``CountedFile`` envuelve un file object binario y delega todas las
operaciones, contabilizando solo lecturas y escrituras con datos.
"""

from __future__ import annotations


class DiskCounter:
    """Acumula lecturas y escrituras físicas de páginas."""

    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0

    def count_read(self) -> None:
        self.reads += 1

    def count_write(self) -> None:
        self.writes += 1

    def reset(self) -> None:
        self.reads = 0
        self.writes = 0

    def snapshot(self) -> dict:
        return {"reads": self.reads, "writes": self.writes}


class CountedFile:
    """Envoltura de un file object binario que cuenta reads/writes.

    Suma 1 read por ``read()``/``readinto()`` que devuelva más de 0 bytes
    y 1 write por ``write()`` con más de 0 bytes. Todo lo demás (``seek``,
    ``flush``, ``tell``, atributos como ``closed``) se delega sin contar.
    """

    def __init__(self, fileobj, counter: DiskCounter) -> None:
        self._fileobj = fileobj
        self._counter = counter

    def read(self, size: int = -1) -> bytes:
        data = self._fileobj.read(size)
        if len(data) > 0:
            self._counter.count_read()
        return data

    def readinto(self, b) -> int:
        n = self._fileobj.readinto(b)
        if n and n > 0:
            self._counter.count_read()
        return n

    def write(self, data) -> int:
        if len(data) > 0:
            self._counter.count_write()
        return self._fileobj.write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fileobj.seek(offset, whence)

    def tell(self) -> int:
        return self._fileobj.tell()

    def flush(self) -> None:
        self._fileobj.flush()

    def truncate(self, size: int | None = None) -> int:
        return self._fileobj.truncate(size)

    def fileno(self) -> int:
        return self._fileobj.fileno()

    def close(self) -> None:
        self._fileobj.close()

    def __getattr__(self, name: str):
        return getattr(self._fileobj, name)
