"""Pruebas del módulo vectorial: tipo VECTOR(n), índice IVF en disco,
operadores de distancia (<->, <=>), SET ivf.probes y planificador
híbrido (pre-filtering / post-filtering / ANN / exacto)."""

import random

import numpy as np
import pytest

from app.engine.executor import Engine
from app.engine.parser import parse
from app.indexes.ivf import IVFIndex
from app.storage.record import (
    Column,
    SerializationError,
    TYPE_VECTOR,
    coerce_vector,
    deserialize_row,
    serialize_row,
)


# ----------------------------------------------------------------------
# Tipo VECTOR(n)
# ----------------------------------------------------------------------
class TestTipoVector:
    def test_coerce_desde_lista_y_string(self):
        col = Column("emb", TYPE_VECTOR, 3)
        assert coerce_vector([1, 0.5, -2], col) == (1.0, 0.5, -2.0)
        assert coerce_vector("[1, 0.5, -2]", col) == (1.0, 0.5, -2.0)

    def test_coerce_dimension_incorrecta(self):
        col = Column("emb", TYPE_VECTOR, 3)
        with pytest.raises(SerializationError):
            coerce_vector([1, 2], col)
        with pytest.raises(SerializationError):
            coerce_vector("no es un vector", col)

    def test_serializacion_roundtrip(self):
        cols = [Column("id", "INT"), Column("emb", TYPE_VECTOR, 4)]
        data = serialize_row(cols, [7, [0.1, -0.2, 0.3, 0.4]])
        assert len(data) == 4 + 16
        row = deserialize_row(cols, data)
        assert row[0] == 7
        assert row[1] == pytest.approx((0.1, -0.2, 0.3, 0.4), abs=1e-7)


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------
class TestParserVectorial:
    def test_create_table_vector(self):
        ast = parse("CREATE TABLE t (id INT PRIMARY KEY, emb VECTOR(512))")
        assert ast["columns"][1] == {"name": "emb", "type": "VECTOR",
                                     "size": 512, "primary_key": False}

    def test_create_index_ivf_con_params(self):
        ast = parse("CREATE INDEX ON t (emb) USING IVF "
                    "WITH (lists = 100, metric = 'COSINE')")
        assert ast["index_type"] == "IVF"
        assert ast["params"] == {"lists": 100, "metric": "COSINE"}

    def test_distancia_vectorial_en_select(self):
        ast = parse("SELECT id, emb <=> '[0.1, 0.2]' AS dist FROM t "
                    "ORDER BY dist ASC LIMIT 5")
        item = ast["columns"][1]
        assert item["vdist"] == {"column": "emb", "op": "<=>",
                                 "query": "[0.1, 0.2]"}
        assert item["alias"] == "dist"
        assert ast["order_by"] == "dist" and ast["order_dir"] == "asc"

    def test_set_ivf_probes(self):
        ast = parse("SET ivf.probes = 5")
        assert ast == {"type": "set", "param": "ivf.probes", "value": 5}


# ----------------------------------------------------------------------
# Índice IVF en disco
# ----------------------------------------------------------------------
def _dataset(n=300, dim=8, seed=7):
    rng = np.random.default_rng(seed)
    centers = np.array([[10.0] * dim, [-10.0] * dim, [0.0] * dim],
                       dtype=np.float32)
    data = []
    for i in range(n):
        c = centers[i % 3] + rng.normal(0, 0.5, dim).astype(np.float32)
        data.append(((i // 256, i % 256), tuple(float(x) for x in c)))
    return data, centers


@pytest.fixture()
def ivf(tmp_path):
    data, centers = _dataset()
    idx = IVFIndex(str(tmp_path / "t_emb"), create=True)
    idx.build(data, dim=8, n_clusters=3, metric="L2")
    idx.close()
    idx = IVFIndex(str(tmp_path / "t_emb"))
    yield idx, data, centers
    idx.close()


class TestIVFIndex:
    def test_persistencia_directorio(self, ivf):
        idx, data, _c = ivf
        assert idx.count == 300
        assert len(idx.clusters) == 3
        assert sum(c[2] for c in idx.clusters) == 300

    def test_busqueda_encuentra_el_cluster_correcto(self, ivf):
        idx, data, centers = ivf
        res = idx.search(tuple(float(x) for x in centers[0]),
                         probes=1, k=10)
        assert len(res) == 10
        # Todos los candidatos deben ser del grupo 0 (i % 3 == 0)
        for (page, slot), _d in res:
            assert (page * 256 + slot) % 3 == 0
        assert [d for _r, d in res] == sorted(d for _r, d in res)

    def test_probes_completo_aproxima_al_exacto(self, ivf):
        idx, data, centers = ivf
        q = tuple(float(x) for x in centers[1] + 0.1)
        exact = sorted(((rid, float(np.linalg.norm(
            np.array(q) - np.array(v)))) for rid, v in data),
            key=lambda t: t[1])[:10]
        approx = idx.search(q, probes=3, k=10)
        assert {r for r, _d in approx} == {r for r, _d in exact}

    def test_add_y_delete(self, ivf):
        idx, data, centers = ivf
        nuevo = tuple(float(x) for x in centers[2])
        idx.add((9, 9), nuevo)
        assert idx.count == 301
        res = idx.search(nuevo, probes=1, k=1)
        assert res[0][0] == (9, 9)
        assert idx.delete((9, 9), nuevo)
        res = idx.search(nuevo, probes=3, k=300)
        assert all(r != (9, 9) for r, _d in res)
        assert idx.count == 300

    def test_metrica_coseno(self, tmp_path):
        vecs = [((0, i), (1.0, 0.1) if i < 50 else (0.1, 1.0))
                for i in range(100)]
        idx = IVFIndex(str(tmp_path / "c_emb"), create=True)
        idx.build(vecs, dim=2, n_clusters=2, metric="COSINE")
        idx.close()
        idx = IVFIndex(str(tmp_path / "c_emb"))
        res = idx.search((1.0, 0.0), probes=2, k=5)
        assert all(r[1] < 50 for r, _d in res)
        assert res[0][1] == pytest.approx(0.0, abs=0.05)
        idx.close()


# ----------------------------------------------------------------------
# Integración SQL en el motor
# ----------------------------------------------------------------------
@pytest.fixture()
def engine(tmp_path):
    random.seed(3)
    eng = Engine(str(tmp_path))
    eng.execute("CREATE TABLE items (nombre VARCHAR(30), "
                "categoria VARCHAR(20), precio FLOAT, emb VECTOR(4))")
    cats = {"footwear": 0.0, "tshirt": 10.0, "watch": -10.0}
    names = list(cats)
    for i in range(150):
        c = names[i % 3]
        v = [cats[c] + random.gauss(0, 0.2) for _ in range(4)]
        eng.execute(f"INSERT INTO items VALUES "
                    f"('item{i}', '{c}', {40 + i % 100}.0, '{v}')")
    return eng


Q = str([0.05] * 4)


class TestEngineVectorial:
    def test_create_index_ivf(self, engine):
        r = engine.execute(
            "CREATE INDEX i ON items (emb) USING IVF "
            "WITH (lists = 3, metric = 'COSINE')")
        assert r["ok"], r.get("error")
        meta = engine.catalog.index_on("items", "emb", {"IVF"})
        assert meta["params"]["lists"] == 3

    def test_ivf_requiere_vector(self, engine):
        r = engine.execute("CREATE INDEX i ON items (precio) USING IVF")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_ann_puro(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        r = engine.execute(
            f"SELECT id, nombre, emb <=> '{Q}' AS dist FROM items "
            f"ORDER BY dist ASC LIMIT 5")
        assert r["ok"], r.get("error")
        assert r["columns"] == ["id", "nombre", "dist"]
        dists = [row[2] for row in r["rows"]]
        assert dists == sorted(dists)
        assert any("ANN puro" in s["detail"] for s in r["plan"]
                   if s["name"] == "Vector Strategy")

    def test_pre_filtering_con_indice_relacional(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        engine.execute("CREATE INDEX c ON items (categoria) USING HASH")
        r = engine.execute(
            f"SELECT id, emb <=> '{Q}' AS dist FROM items "
            f"WHERE categoria = 'footwear' ORDER BY dist ASC LIMIT 5")
        assert r["ok"], r.get("error")
        assert any("Pre-filtering" in s["detail"] for s in r["plan"]
                   if s["name"] == "Vector Strategy")

    def test_post_filtering_sin_indice_relacional(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        r = engine.execute(
            f"SELECT id, precio, emb <=> '{Q}' AS dist FROM items "
            f"WHERE precio <= 45.0 ORDER BY dist ASC LIMIT 5")
        assert r["ok"], r.get("error")
        assert all(row[1] <= 45.0 for row in r["rows"])
        assert any("Post-filtering" in s["detail"] for s in r["plan"]
                   if s["name"] == "Vector Strategy")

    def test_exact_scan_sin_indices(self, engine):
        r = engine.execute(
            f"SELECT id, emb <-> '{Q}' AS dist FROM items "
            f"ORDER BY dist ASC LIMIT 5")
        assert r["ok"], r.get("error")
        assert any("Exact Vector Scan" in s["detail"] for s in r["plan"]
                   if s["name"] == "Vector Strategy")

    def test_exact_scan_es_ground_truth(self, engine):
        # El escaneo exacto debe devolver los 5 más cercanos reales.
        r = engine.execute(
            f"SELECT id, emb <-> '{Q}' AS dist FROM items "
            f"ORDER BY dist ASC LIMIT 5")
        rows = engine.execute("SELECT id, emb FROM items LIMIT 200")
        q = np.array([0.05] * 4, dtype=np.float32)
        esperado = sorted(
            ((row[0], float(np.linalg.norm(q - np.array(row[1]))))
             for row in rows["rows"]), key=lambda t: t[1])[:5]
        assert [row[0] for row in r["rows"]] == [e[0] for e in esperado]

    def test_set_probes_afecta_al_plan(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        engine.execute("SET ivf.probes = 3")
        r = engine.execute(
            f"SELECT id, emb <=> '{Q}' AS dist FROM items LIMIT 5")
        assert any("probes=3" in s["detail"] for s in r["plan"]
                   if s["name"] == "IVF ANN Scan")

    def test_metrica_del_operador_debe_coincidir(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        r = engine.execute(
            f"SELECT id, emb <-> '{Q}' AS dist FROM items LIMIT 5")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_vector_no_puede_ser_pk(self, engine):
        r = engine.execute(
            "CREATE TABLE mal (emb VECTOR(4) PRIMARY KEY)")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_mantenimiento_insert_delete(self, engine):
        engine.execute("CREATE INDEX i ON items (emb) USING IVF "
                       "WITH (lists = 3, metric = 'COSINE')")
        v = str([0.01] * 4)
        engine.execute(f"INSERT INTO items VALUES ('nuevo', 'footwear', "
                       f"10.0, '{v}')")
        r = engine.execute(
            f"SELECT id, nombre, emb <=> '{Q}' AS dist FROM items "
            f"ORDER BY dist ASC LIMIT 1")
        assert r["rows"][0][1] == "nuevo"
        rid_id = r["rows"][0][0]
        engine.execute(f"DELETE FROM items WHERE id = {rid_id}")
        r = engine.execute(
            f"SELECT id, nombre, emb <=> '{Q}' AS dist FROM items "
            f"ORDER BY dist ASC LIMIT 1")
        assert r["rows"][0][1] != "nuevo"
