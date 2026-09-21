"""Pruebas del B+ Tree: punto, rango, duplicados, splits y persistencia."""

import random
import struct

import pytest

from app.indexes.btree import BPlusTree


def make_tree(tmp_path, key_size=4):
    return BPlusTree(
        str(tmp_path / "t.btree"),
        key_size,
        lambda v: struct.pack("<i", v),
        lambda b: struct.unpack("<i", b)[0],
        create=True,
    )


class TestBPlusTree:
    def test_insert_y_search_simple(self, tmp_path):
        with make_tree(tmp_path) as t:
            t.insert(10, (1, 0))
            t.insert(20, (1, 1))
            assert t.search(10) == [(1, 0)]
            assert t.search(20) == [(1, 1)]
            assert t.search(99) == []

    def test_mil_claves_con_splits(self, tmp_path):
        """1000 claves aleatorias fuerzan splits de hojas e internos."""
        rng = random.Random(42)
        keys = rng.sample(range(10_000), 1000)
        with make_tree(tmp_path) as t:
            for i, k in enumerate(keys):
                t.insert(k, (i, i % 8))
            for i, k in enumerate(keys):
                assert t.search(k) == [(i, i % 8)]
            assert t.page_count > 2  # hubo splits

    def test_range_search(self, tmp_path):
        rng = random.Random(7)
        keys = sorted(rng.sample(range(5000), 800))
        with make_tree(tmp_path) as t:
            for i, k in enumerate(keys):
                t.insert(k, (1, i))
            lo, hi = keys[100], keys[200]
            esperados = {(1, i) for i in range(100, 201)}
            assert set(t.range_search(lo, hi)) == esperados

    def test_range_search_extremos_abiertos(self, tmp_path):
        with make_tree(tmp_path) as t:
            for k in range(0, 100, 10):
                t.insert(k, (1, k))
            assert set(t.range_search(hi=30)) == {(1, 0), (1, 10), (1, 20), (1, 30)}
            assert set(t.range_search(hi=30, hi_inc=False)) == {
                (1, 0), (1, 10), (1, 20)}
            assert set(t.range_search(lo=50)) == {(1, k) for k in range(50, 100, 10)}
            assert set(t.range_search(lo=50, lo_inc=False)) == {
                (1, k) for k in range(60, 100, 10)}

    def test_duplicados(self, tmp_path):
        with make_tree(tmp_path) as t:
            for i in range(300):
                t.insert(5, (i, 0))  # misma clave, muchos RIDs
            t.insert(4, (999, 0))
            t.insert(6, (998, 0))
            assert set(t.search(5)) == {(i, 0) for i in range(300)}
            assert t.search(4) == [(999, 0)]
            assert set(t.range_search(5, 5)) == {(i, 0) for i in range(300)}

    def test_delete(self, tmp_path):
        rng = random.Random(1)
        keys = rng.sample(range(2000), 500)
        with make_tree(tmp_path) as t:
            for i, k in enumerate(keys):
                t.insert(k, (i, 1))
            borrados = keys[:100]
            for i, k in enumerate(borrados):
                t.delete(k, (i, 1))
            for i, k in enumerate(borrados):
                assert t.search(k) == []
            for i, k in enumerate(keys[100:], start=100):
                assert t.search(k) == [(i, 1)]
            with pytest.raises(KeyError):
                t.delete(keys[0], (0, 1))

    def test_entrada_duplicada_falla(self, tmp_path):
        with make_tree(tmp_path) as t:
            t.insert(1, (1, 1))
            with pytest.raises(KeyError):
                t.insert(1, (1, 1))

    def test_persistencia(self, tmp_path):
        rng = random.Random(9)
        keys = rng.sample(range(3000), 400)
        path = str(tmp_path / "t.btree")
        with BPlusTree(path, 4, lambda v: struct.pack("<i", v),
                       lambda b: struct.unpack("<i", b)[0], create=True) as t:
            for i, k in enumerate(keys):
                t.insert(k, (i, 2))
        with BPlusTree(path, 4, lambda v: struct.pack("<i", v),
                       lambda b: struct.unpack("<i", b)[0]) as t:
            for i, k in enumerate(keys):
                assert t.search(k) == [(i, 2)]
            assert set(t.range_search(0, 100)) == {
                (i, 2) for i, k in enumerate(keys) if 0 <= k <= 100}


class TestPageSizeYAltura:
    """``page_size`` parametrizable (experimento de tamaño de bloque) y
    el método público ``height()``."""

    ENC = staticmethod(lambda v: struct.pack("<i", v))
    DEC = staticmethod(lambda b: struct.unpack("<i", b)[0])

    def test_pagina_mas_chica_reduce_capacidades(self, tmp_path):
        with make_tree(tmp_path, key_size=4) as big:
            cap_hoja_4k, cap_int_4k = big.leaf_cap, big.internal_cap
        with BPlusTree(str(tmp_path / "t1k.btree"), 4, self.ENC, self.DEC,
                       create=True, page_size=1024) as small:
            assert small.leaf_cap < cap_hoja_4k
            assert small.internal_cap < cap_int_4k
            # (1024 - header) // (key + rid) para hoja de claves INT
            assert small.leaf_cap == (1024 - 11) // (4 + 6)

    def test_insertar_buscar_y_reabrir_con_otro_page_size(self, tmp_path):
        rng = random.Random(7)
        keys = rng.sample(range(5000), 500)
        path = str(tmp_path / "t2k.btree")
        with BPlusTree(path, 4, self.ENC, self.DEC,
                       create=True, page_size=2048) as t:
            for i, k in enumerate(keys):
                t.insert(k, (i, 0))
            for i, k in enumerate(keys):
                assert t.search(k) == [(i, 0)]
        with BPlusTree(path, 4, self.ENC, self.DEC, page_size=2048) as t:
            for i, k in enumerate(keys):
                assert t.search(k) == [(i, 0)]
            assert t.height() >= 2

    def test_page_size_invalido(self, tmp_path):
        with pytest.raises(ValueError):
            BPlusTree(str(tmp_path / "x.btree"), 4, self.ENC, self.DEC,
                      create=True, page_size=8)

    def test_height(self, tmp_path):
        with make_tree(tmp_path) as t:
            assert t.height() == 1  # solo la raíz hoja
            for i in range(5000):
                t.insert(i, (i, 0))
            h = t.height()
            # 5000 claves INT ordenadas en 4 KB: ~13 hojas llenas bajo
            # una raíz interna (hoja_cap=408, interno_cap=292).
            assert h == 2
            # altura = niveles del camino raíz->hoja; consistente con
            # que una búsqueda lee exactamente h páginas de nodos.
            assert t.search(4999) == [(4999, 0)]


class TestDobleEnlaceHojas:
    """Los splits mantienen la lista doblemente enlazada (next/prev)."""

    def _hojas(self, t):
        """Ids de las hojas en orden, siguiendo ``next`` desde la izquierda."""
        ids = []
        pid = t._leftmost_leaf()
        while pid:
            leaf = t._load_node(pid)
            ids.append(pid)
            pid = leaf.next
        return ids

    def test_prev_y_next_consistentes_tras_splits(self, tmp_path):
        rng = random.Random(3)
        keys = rng.sample(range(10_000), 1000)
        with make_tree(tmp_path) as t:
            for i, k in enumerate(keys):
                t.insert(k, (i, 0))
            ids = self._hojas(t)
            assert len(ids) > 2  # hubo varios splits
            assert t._load_node(ids[0]).prev == 0
            for i, pid in enumerate(ids):
                leaf = t._load_node(pid)
                esperado_prev = ids[i - 1] if i > 0 else 0
                esperado_next = ids[i + 1] if i + 1 < len(ids) else 0
                assert leaf.prev == esperado_prev
                assert leaf.next == esperado_next

    def test_recorrido_hacia_atras_con_prev(self, tmp_path):
        with make_tree(tmp_path) as t:
            for i in range(500):
                t.insert(i, (i, 0))
            ids = self._hojas(t)
            # desde la última hoja hacia la primera, por prev
            atras = []
            pid = ids[-1]
            while pid:
                atras.append(pid)
                pid = t._load_node(pid).prev
            assert atras == ids[::-1]

    def test_prev_sobrevive_reapertura(self, tmp_path):
        path = str(tmp_path / "t.btree")
        with BPlusTree(path, 4, lambda v: struct.pack("<i", v),
                       lambda b: struct.unpack("<i", b)[0], create=True) as t:
            for i in range(500):
                t.insert(i, (i, 0))
        with BPlusTree(path, 4, lambda v: struct.pack("<i", v),
                       lambda b: struct.unpack("<i", b)[0]) as t:
            ids = self._hojas(t)
            assert len(ids) > 1
            for i, pid in enumerate(ids):
                assert t._load_node(pid).prev == (ids[i - 1] if i > 0 else 0)
