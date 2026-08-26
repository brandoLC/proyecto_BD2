"""Pruebas del R-Tree contra una referencia de fuerza bruta."""

import math
import random

import pytest

from app.indexes.rtree import RTree, dist


def brute_radius(points, center, r):
    return {rid for p, rid in points if dist(p, center) <= r}


def brute_knn(points, center, k):
    orden = sorted(points, key=lambda pr: dist(pr[0], center))
    return [(rid, dist(p, center)) for p, rid in orden[:k]]


class TestRTree:
    def test_insert_y_radius_simple(self, tmp_path):
        with RTree(str(tmp_path / "t.rtree"), max_entries=8, create=True) as t:
            t.insert((0.0, 0.0), (1, 0))
            t.insert((3.0, 4.0), (1, 1))
            t.insert((100.0, 100.0), (1, 2))
            assert set(t.search_radius((0.0, 0.0), 5.0)) == {(1, 0), (1, 1)}
            assert t.search_radius((1000.0, 1000.0), 1.0) == []

    def test_radius_vs_fuerza_bruta(self, tmp_path):
        rng = random.Random(21)
        points = [((rng.uniform(-500, 500), rng.uniform(-500, 500)),
                   (i, i % 16)) for i in range(400)]
        with RTree(str(tmp_path / "t.rtree"), max_entries=8, create=True) as t:
            for p, rid in points:
                t.insert(p, rid)
            assert t.page_count > 2  # hubo splits cuadráticos
            for _ in range(10):
                center = (rng.uniform(-500, 500), rng.uniform(-500, 500))
                r = rng.uniform(10, 300)
                assert set(t.search_radius(center, r)) == \
                    brute_radius(points, center, r)

    def test_knn_vs_fuerza_bruta(self, tmp_path):
        rng = random.Random(33)
        points = [((rng.uniform(-100, 100), rng.uniform(-100, 100)),
                   (i, 0)) for i in range(300)]
        with RTree(str(tmp_path / "t.rtree"), max_entries=8, create=True) as t:
            for p, rid in points:
                t.insert(p, rid)
            for _ in range(10):
                center = (rng.uniform(-100, 100), rng.uniform(-100, 100))
                k = rng.randint(1, 20)
                resultado = t.knn(center, k)
                esperado = brute_knn(points, center, k)
                assert [rid for rid, _ in resultado] == \
                    [rid for rid, _ in esperado]
                for (_, d1), (_, d2) in zip(resultado, esperado):
                    assert math.isclose(d1, d2)

    def test_delete(self, tmp_path):
        rng = random.Random(8)
        points = [((rng.uniform(0, 100), rng.uniform(0, 100)), (i, 0))
                  for i in range(100)]
        with RTree(str(tmp_path / "t.rtree"), max_entries=8, create=True) as t:
            for p, rid in points:
                t.insert(p, rid)
            for p, rid in points[:30]:
                t.delete(p, rid)
            restantes = points[30:]
            center, r = (50.0, 50.0), 40.0
            assert set(t.search_radius(center, r)) == brute_radius(
                restantes, center, r)
            with pytest.raises(KeyError):
                t.delete(points[0][0], points[0][1])

    def test_persistencia(self, tmp_path):
        rng = random.Random(13)
        points = [((rng.uniform(0, 50), rng.uniform(0, 50)), (i, 1))
                  for i in range(150)]
        path = str(tmp_path / "t.rtree")
        with RTree(path, max_entries=8, create=True) as t:
            for p, rid in points:
                t.insert(p, rid)
        with RTree(path) as t:
            center, r = (25.0, 25.0), 15.0
            assert set(t.search_radius(center, r)) == brute_radius(
                points, center, r)
            assert [rid for rid, _ in t.knn(center, 5)] == \
                [rid for rid, _ in brute_knn(points, center, 5)]
