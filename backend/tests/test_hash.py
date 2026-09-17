"""Pruebas del hash extensible: crecimiento, búsqueda, borrado y disco."""

import random
import struct

import pytest

from app.indexes.extendible_hash import ExtendibleHash


def make_hash(tmp_path):
    return ExtendibleHash(
        str(tmp_path / "t.hash"),
        4,
        lambda v: struct.pack("<i", v),
        lambda b: struct.unpack("<i", b)[0],
        create=True,
    )


class TestExtendibleHash:
    def test_insert_y_search(self, tmp_path):
        with make_hash(tmp_path) as h:
            h.insert(10, (1, 0))
            h.insert(20, (1, 1))
            assert h.search(10) == [(1, 0)]
            assert h.search(20) == [(1, 1)]
            assert h.search(30) == []

    def test_crecimiento_del_directorio(self, tmp_path):
        """Insertar muchas claves fuerza splits y duplica el directorio."""
        rng = random.Random(3)
        keys = rng.sample(range(50_000), 1500)
        with make_hash(tmp_path) as h:
            g0 = h.global_depth
            for i, k in enumerate(keys):
                h.insert(k, (i, i % 4))
            assert h.global_depth > g0
            assert h.num_buckets > 2
            for i, k in enumerate(keys):
                assert h.search(k) == [(i, i % 4)]

    def test_duplicados_misma_clave(self, tmp_path):
        with make_hash(tmp_path) as h:
            for i in range(10):
                h.insert(7, (i, 0))
            assert set(h.search(7)) == {(i, 0) for i in range(10)}

    def test_entrada_duplicada_falla(self, tmp_path):
        with make_hash(tmp_path) as h:
            h.insert(1, (1, 1))
            with pytest.raises(KeyError):
                h.insert(1, (1, 1))

    def test_delete(self, tmp_path):
        rng = random.Random(11)
        keys = rng.sample(range(20_000), 800)
        with make_hash(tmp_path) as h:
            for i, k in enumerate(keys):
                h.insert(k, (i, 5))
            for i, k in enumerate(keys[:200]):
                h.delete(k, (i, 5))
            for i, k in enumerate(keys[:200]):
                assert h.search(k) == []
            for i, k in enumerate(keys[200:], start=200):
                assert h.search(k) == [(i, 5)]
            with pytest.raises(KeyError):
                h.delete(keys[0], (0, 5))

class TestExtendibleHashIO:
    """El formato EXH2 pagina bajo demanda: búsqueda e inserción tocan
    O(1) bloques, no el archivo completo (regresión del EXH1)."""

    def test_busqueda_puntual_lee_pocos_bloques(self, tmp_path):
        keys = random.Random(9).sample(range(200_000), 10_000)
        with make_hash(tmp_path) as h:
            h.defer_flush = True
            for i, k in enumerate(keys):
                h.insert(k, (i, 0))
            h.flush()

        # Instancia fría instrumentada: cabecera + página de directorio
        # + página de bucket.
        from app.storage.disk_counter import DiskCounter
        counter = DiskCounter()
        with ExtendibleHash(
                str(tmp_path / "t.hash"), 4,
                lambda v: struct.pack("<i", v),
                lambda b: struct.unpack("<i", b)[0],
                counter=counter) as h:
            assert h.search(keys[0]) == [(0, 0)]
            assert counter.reads <= 6
            counter.reset()
            assert h.search(keys[5000]) == [(5000, 0)]
            assert counter.reads <= 3  # cachés calientes: dir + bucket

    def test_insercion_escribe_paginas_afectadas(self, tmp_path):
        from app.storage.disk_counter import DiskCounter
        counter = DiskCounter()
        with ExtendibleHash(
                str(tmp_path / "t.hash"), 4,
                lambda v: struct.pack("<i", v),
                lambda b: struct.unpack("<i", b)[0],
                create=True, counter=counter) as h:
            # Inserción sin split: solo la página del bucket.
            counter.reset()
            h.insert(12345, (1, 0))
            assert counter.writes == 1
            # Amortizado: 5000 inserciones escriben ~1 página cada una
            # (el EXH1 reescribía el archivo completo por inserción).
            counter.reset()
            for i in range(5000):
                h.insert(i, (i, 0))
            assert counter.writes < 2 * 5000

    def test_persistencia(self, tmp_path):
        rng = random.Random(5)
        keys = rng.sample(range(30_000), 600)
        path = str(tmp_path / "t.hash")
        with ExtendibleHash(path, 4, lambda v: struct.pack("<i", v),
                            lambda b: struct.unpack("<i", b)[0],
                            create=True) as h:
            for i, k in enumerate(keys):
                h.insert(k, (i, 3))
            depth = h.global_depth
        with ExtendibleHash(path, 4, lambda v: struct.pack("<i", v),
                            lambda b: struct.unpack("<i", b)[0]) as h:
            assert h.global_depth == depth
            for i, k in enumerate(keys):
                assert h.search(k) == [(i, 3)]
