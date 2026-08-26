"""Prueba de extremo a extremo del ejecutor sobre un directorio temporal."""

import math

import pytest

from app.engine.executor import Engine


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


def q(engine, sql):
    return engine.execute(sql)


def plan_names(result):
    return [s["name"] for s in result["plan"]]


class TestFlujoCompleto:
    def test_create_insert_select_delete(self, engine):
        r = q(engine, "CREATE TABLE rest (id INT PRIMARY KEY, "
                      "nombre VARCHAR(30), precio FLOAT, ubicacion POINT);")
        assert r["ok"] and r["kind"] == "create_table"
        assert "Parse SQL" in plan_names(r)

        # La PK ya trae un B+ Tree automático (rest_id_pk); crear otro
        # BTREE sobre la misma columna se rechaza como duplicado.
        r = q(engine, "CREATE INDEX idx_id ON rest (id) USING BTREE;")
        assert not r["ok"] and "ya existe" in r["error"]
        r = q(engine, "CREATE INDEX idx_nom ON rest (nombre) USING HASH;")
        assert r["ok"]
        r = q(engine, "CREATE INDEX idx_ubi ON rest (ubicacion) USING RTREE;")
        assert r["ok"]

        datos = [
            (1, "La Mar", 80.0, (-12.06, -77.03)),
            (2, "Central", 95.5, (-12.05, -77.04)),
            (3, "Maido", 70.0, (-12.10, -77.05)),
            (4, "Rafael", 60.0, (-12.07, -77.02)),
            (5, "Astrid", 55.0, (-12.08, -77.01)),
        ]
        for id_, nombre, precio, (x, y) in datos:
            r = q(engine, f"INSERT INTO rest VALUES ({id_}, '{nombre}', "
                          f"{precio}, ({x}, {y}));")
            assert r["ok"] and r["rowcount"] == 1
            assert "Heap Insert" in plan_names(r)
            assert "Index Maintenance" in plan_names(r)

        # SELECT por PK: debe usar el B+ Tree
        r = q(engine, "SELECT * FROM rest WHERE id = 3;")
        assert r["ok"] and r["rows"][0][1] == "Maido"
        assert "Index Scan" in plan_names(r)
        detalle = next(s["detail"] for s in r["plan"]
                       if s["name"] == "Index Scan")
        assert "USING BTREE ON rest.id" in detalle

        # SELECT por igualdad en columna con HASH
        r = q(engine, "SELECT id, nombre FROM rest WHERE nombre = 'Rafael';")
        assert r["ok"] and r["rows"] == [[4, "Rafael"]]
        detalle = next(s["detail"] for s in r["plan"]
                       if s["name"] == "Index Scan")
        assert "USING HASH ON rest.nombre" in detalle

        # SELECT por rango: B+ tree range search
        r = q(engine, "SELECT id FROM rest WHERE id BETWEEN 2 AND 4;")
        assert r["ok"] and sorted(row[0] for row in r["rows"]) == [2, 3, 4]
        assert "Index Range Scan" in plan_names(r)

        # SELECT con comparador unilateral sobre el BTREE
        r = q(engine, "SELECT id FROM rest WHERE id >= 4;")
        assert sorted(row[0] for row in r["rows"]) == [4, 5]

        # SELECT sin índice: Sequential Scan
        r = q(engine, "SELECT id FROM rest WHERE precio < 60.0;")
        assert r["ok"] and r["rows"] == [[5]]
        assert "Sequential Scan" in plan_names(r)

        # SELECT con LIMIT
        r = q(engine, "SELECT id FROM rest LIMIT 2;")
        assert r["ok"] and len(r["rows"]) == 2
        assert "Limit" in plan_names(r)

        # Spatial radius con R-Tree
        r = q(engine, "SELECT * FROM rest WHERE ubicacion "
                      "IN ((-12.06, -77.03), 0.05);")
        assert r["ok"]
        assert "R-Tree Radius Search" in plan_names(r)
        ids = {row[0] for row in r["rows"]}
        assert ids == {1, 2, 3, 4, 5}  # todos están a menos de 0.05
        r = q(engine, "SELECT * FROM rest WHERE ubicacion "
                      "IN ((-12.06, -77.03), 0.02);")
        ids = {row[0] for row in r["rows"]}
        assert ids == {1, 2, 4}  # 3 (d≈0.045) y 5 (d≈0.028) quedan fuera
        assert r["spatial"] is not None
        assert r["spatial"]["column"] == "ubicacion"
        for p in r["spatial"]["points"]:
            d = math.hypot(p["x"] + 12.06, p["y"] + 77.03)
            assert d <= 0.05
            assert p["row"][0] in {1, 2, 3, 4, 5}

        # KNN con R-Tree
        r = q(engine, "SELECT * FROM rest WHERE ubicacion "
                      "KNN ((-12.06, -77.03), 2);")
        assert r["ok"] and len(r["rows"]) == 2
        assert "R-Tree KNN Search" in plan_names(r)
        assert r["rows"][0][0] == 1  # el más cercano es el punto mismo

        # DELETE usando el índice HASH
        r = q(engine, "DELETE FROM rest WHERE nombre = 'Central';")
        assert r["ok"] and r["rowcount"] == 1
        r = q(engine, "SELECT * FROM rest WHERE id = 2;")
        assert r["ok"] and r["rows"] == []
        r = q(engine, "SELECT * FROM rest WHERE ubicacion "
                      "IN ((-12.05, -77.04), 0.001);")
        assert r["rows"] == []

    def test_pk_duplicada_rechazada(self, engine):
        q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v INT);")
        q(engine, "INSERT INTO t VALUES (1, 10);")
        r = q(engine, "INSERT INTO t VALUES (1, 20);")
        assert not r["ok"] and r["stage"] == "execution"

    def test_errores_por_etapa(self, engine):
        r = q(engine, "SELECT * FROM;")
        assert not r["ok"] and r["stage"] == "parse"
        r = q(engine, "SELECT * FROM noexiste;")
        assert not r["ok"] and r["stage"] == "semantic"
        q(engine, "CREATE TABLE t (id INT);")
        r = q(engine, "INSERT INTO t VALUES ('no es int');")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_rtree_solo_en_point(self, engine):
        q(engine, "CREATE TABLE t (id INT, p POINT);")
        r = q(engine, "CREATE INDEX ON t (id) USING RTREE;")
        assert not r["ok"] and r["stage"] == "semantic"
        r = q(engine, "CREATE INDEX ON t (p) USING HASH;")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_select_sin_condicion_espacial_spatial_null(self, engine):
        q(engine, "CREATE TABLE t (id INT);")
        q(engine, "INSERT INTO t VALUES (1);")
        r = q(engine, "SELECT * FROM t;")
        assert r["ok"] and r["spatial"] is None

    def test_table_info(self, engine):
        q(engine, "CREATE TABLE t (id INT PRIMARY KEY, p POINT);")
        q(engine, "INSERT INTO t VALUES (1, (1.0, 2.0));")
        info = engine.table_info()
        assert len(info) == 1
        t = info[0]
        assert t["name"] == "t"
        assert t["rowcount"] == 1
        assert t["columns"][0] == {"name": "id", "type": "INT",
                                   "primary_key": True}
        assert t["columns"][1]["type"] == "POINT"
        # Índice B+ Tree automático de la PRIMARY KEY
        assert t["indexes"] == [{"name": "t_id_pk", "column": "id",
                                 "type": "BTREE"}]
        assert len(t["files"]) == 2
        for f in t["files"]:
            assert f["size_bytes"] > 0 and f["pages"] >= 1
            assert f["size_bytes"] % 4096 == 0

    def test_persistencia_entre_instancias(self, tmp_path):
        data = str(tmp_path / "data")
        e1 = Engine(data)
        q(e1, "CREATE TABLE t (id INT PRIMARY KEY);")
        q(e1, "INSERT INTO t VALUES (42);")
        e2 = Engine(data)  # recarga el catálogo desde disco
        r = q(e2, "SELECT * FROM t WHERE id = 42;")
        assert r["ok"] and r["rows"] == [[42]]
