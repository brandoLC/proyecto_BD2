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


class TestPKImplicita:
    """CREATE TABLE sin PRIMARY KEY declara agrega una columna SERIAL
    autogenerada (``id``, o ``_minidb_id`` si ``id`` ya existe)."""

    def test_create_sin_pk_agrega_serial(self, engine):
        r = q(engine, "CREATE TABLE autos (nombre VARCHAR(30), precio FLOAT);")
        assert r["ok"]
        cols = engine.catalog.columns("autos")
        assert cols[0].name == "id" and cols[0].auto and cols[0].primary_key
        assert [c.name for c in cols] == ["id", "nombre", "precio"]
        info = engine.table_info()[0]
        assert info["columns"][0] == {"name": "id", "type": "SERIAL",
                                      "primary_key": True, "auto": True}
        # La PK lleva su B+ Tree automático (como toda PK explícita)
        assert info["indexes"] == [{"name": "autos_id_pk", "column": "id",
                                    "type": "BTREE"}]

    def test_insert_autoasigna_y_select_muestra(self, engine):
        q(engine, "CREATE TABLE autos (nombre VARCHAR(30));")
        for n in ("La Mar", "Central", "Maido"):
            r = q(engine, f"INSERT INTO autos VALUES ('{n}');")
            assert r["ok"]
        r = q(engine, "SELECT * FROM autos;")
        assert [row[0] for row in r["rows"]] == [1, 2, 3]
        assert r["rows"][1] == [2, "Central"]
        # búsqueda por la PK implícita usa el B+ Tree
        r = q(engine, "SELECT nombre FROM autos WHERE id = 3;")
        assert r["rows"] == [["Maido"]]
        assert "Index Scan" in plan_names(r)

    def test_insert_con_id_explicito_adelanta_secuencia(self, engine):
        q(engine, "CREATE TABLE t (v INT);")
        q(engine, "INSERT INTO t VALUES (10);")      # auto: id = 1
        q(engine, "INSERT INTO t VALUES (50, 20);")  # id explícito 50
        q(engine, "INSERT INTO t VALUES (30);")      # auto: id = 51
        r = q(engine, "SELECT * FROM t;")
        assert [row[0] for row in r["rows"]] == [1, 50, 51]

    def test_id_explicito_duplicado_rechazado(self, engine):
        q(engine, "CREATE TABLE t (v INT);")
        q(engine, "INSERT INTO t VALUES (5, 1);")
        r = q(engine, "INSERT INTO t VALUES (5, 2);")
        assert not r["ok"] and "clave primaria duplicada" in r["error"]

    def test_nombre_reservado_usa_minidb_id(self, engine):
        r = q(engine, "CREATE TABLE t (id VARCHAR(10), v INT);")
        assert r["ok"]
        cols = engine.catalog.columns("t")
        assert cols[0].name == "_minidb_id" and cols[0].auto
        q(engine, "INSERT INTO t VALUES ('abc', 7);")
        r = q(engine, "SELECT * FROM t;")
        assert r["rows"] == [[1, "abc", 7]]

    def test_pk_explicita_no_agrega_columna(self, engine):
        q(engine, "CREATE TABLE t (codigo INT PRIMARY KEY, v INT);")
        cols = engine.catalog.columns("t")
        assert len(cols) == 2 and all(not c.auto for c in cols)
        assert q(engine, "INSERT INTO t VALUES (1, 2);")["ok"]
        # la PK explícita no es autoasignable: faltan valores
        r = q(engine, "INSERT INTO t VALUES (3);")
        assert not r["ok"]

    def test_secuencia_persiste_entre_instancias(self, tmp_path):
        data = str(tmp_path / "data")
        e1 = Engine(data)
        q(e1, "CREATE TABLE t (v INT);")
        q(e1, "INSERT INTO t VALUES (1);")
        q(e1, "INSERT INTO t VALUES (2);")
        e2 = Engine(data)  # recarga catálogo + secuencia desde disco
        q(e2, "INSERT INTO t VALUES (3);")
        r = q(e2, "SELECT * FROM t;")
        assert [row[0] for row in r["rows"]] == [1, 2, 3]

    def test_indices_funcionan_con_pk_implicita(self, engine):
        q(engine, "CREATE TABLE rest (nombre VARCHAR(30), precio FLOAT, "
                  "ubicacion POINT);")
        q(engine, "CREATE INDEX idx_precio ON rest (precio) USING BTREE;")
        q(engine, "CREATE INDEX idx_ubi ON rest (ubicacion) USING RTREE;")
        datos = [("A", 10.0, (-12.0, -77.0)), ("B", 20.0, (-12.1, -77.1)),
                 ("C", 30.0, (-12.2, -77.2))]
        for n, p, (x, y) in datos:
            r = q(engine, f"INSERT INTO rest VALUES ('{n}', {p}, ({x}, {y}));")
            assert r["ok"]
        r = q(engine, "SELECT nombre FROM rest WHERE precio = 20.0;")
        assert r["rows"] == [["B"]]
        assert "Index Scan" in plan_names(r)
        r = q(engine, "SELECT nombre FROM rest WHERE id BETWEEN 1 AND 2;")
        assert sorted(row[0] for row in r["rows"]) == ["A", "B"]
        assert "Index Range Scan" in plan_names(r)
        r = q(engine, "SELECT nombre FROM rest WHERE ubicacion "
                      "KNN ((-12.0, -77.0), 1);")
        assert r["rows"][0] == ["A"]
        assert "R-Tree KNN Search" in plan_names(r)

    def test_tabla_duplicada_error_claro(self, engine):
        q(engine, "CREATE TABLE t (v INT);")
        r = q(engine, "CREATE TABLE t (v INT);")
        assert not r["ok"] and r["stage"] == "semantic"
        assert r["error"] == "la tabla 't' ya existe"


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
                                   "primary_key": True, "auto": False}
        assert t["columns"][1]["type"] == "POINT"
        # Índice B+ Tree automático de la PRIMARY KEY
        assert t["indexes"] == [{"name": "t_id_pk", "column": "id",
                                 "type": "BTREE"}]
        assert len(t["files"]) == 2
        for f in t["files"]:
            assert f["size_bytes"] > 0 and f["pages"] >= 1
            assert f["size_bytes"] % 4096 == 0

    def test_drop_table(self, engine, tmp_path):
        q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20));")
        q(engine, "CREATE INDEX ON t (v) USING HASH;")
        q(engine, "INSERT INTO t VALUES (1, 'a');")
        heap = tmp_path / "data" / "t.heap"
        idx_pk = tmp_path / "data" / "t_id.btree"
        idx_v = tmp_path / "data" / "t_v.hash"
        assert heap.exists() and idx_pk.exists() and idx_v.exists()

        r = q(engine, "DROP TABLE t;")
        assert r["ok"] and r["kind"] == "drop_table"
        assert not heap.exists() and not idx_pk.exists() and not idx_v.exists()
        assert engine.table_info() == []

        # consultar la tabla eliminada -> error semántico
        r = q(engine, "SELECT * FROM t;")
        assert not r["ok"] and r["stage"] == "semantic"
        # eliminarla de nuevo -> error semántico
        r = q(engine, "DROP TABLE t;")
        assert not r["ok"] and r["stage"] == "semantic"
        # se puede recrear con el mismo nombre (flujo CSV: drop + recreate)
        r = q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v TEXT);")
        assert r["ok"]

    def test_persistencia_entre_instancias(self, tmp_path):
        data = str(tmp_path / "data")
        e1 = Engine(data)
        q(e1, "CREATE TABLE t (id INT PRIMARY KEY);")
        q(e1, "INSERT INTO t VALUES (42);")
        e2 = Engine(data)  # recarga el catálogo desde disco
        r = q(e2, "SELECT * FROM t WHERE id = 42;")
        assert r["ok"] and r["rows"] == [[42]]
