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
