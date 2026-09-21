"""Pruebas de la conjunción AND en el WHERE: parser, planificador
(elección del mejor acceso + filtro residual) y resultados."""

import pytest

from app.engine.executor import Engine
from app.engine.parser import parse


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


def q(engine, sql):
    r = engine.execute(sql)
    assert r["ok"], r.get("error")
    return r


def plan_names(result):
    return [s["name"] for s in result["plan"]]


def tabla_heap(engine, n=30):
    """Tabla HEAP con PK (B+ automático) y columnas v (con BTREE) y w."""
    q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v INT, w INT);")
    q(engine, "CREATE INDEX idx_v ON t (v) USING BTREE;")
    for k in range(n):
        q(engine, f"INSERT INTO t VALUES ({k}, {k * 10}, {k % 3});")


class TestParserAnd:
    def test_dos_condiciones(self):
        r = parse("SELECT * FROM empleados WHERE id >= 100 AND id <= 500;")
        where = r["where"]
        assert where["kind"] == "and"
        assert [c["op"] for c in where["conditions"]] == [">=", "<="]
        assert all(c["column"] == "id" for c in where["conditions"])

    def test_tres_condiciones(self):
        r = parse("SELECT * FROM t WHERE a = 1 AND b < 2 AND c >= 3;")
        assert r["where"]["kind"] == "and"
        assert len(r["where"]["conditions"]) == 3

    def test_between_con_and_externo(self):
        # El AND interno del BETWEEN no debe tragarse la conjunción.
        r = parse("SELECT * FROM t WHERE x BETWEEN 1 AND 5 AND y = 3;")
        where = r["where"]
        assert where["kind"] == "and"
        assert [c["kind"] for c in where["conditions"]] == ["between",
                                                            "compare"]
        assert where["conditions"][0]["low"] == 1
        assert where["conditions"][0]["high"] == 5

    def test_sin_and_conserva_ast_plano(self):
        r = parse("SELECT * FROM t WHERE id = 7;")
        assert r["where"]["kind"] == "compare"


class TestAndConIndices:
    def test_rango_con_btree(self, engine):
        tabla_heap(engine)
        r = q(engine, "SELECT id FROM t WHERE v >= 100 AND v <= 200;")
        assert "Index Range Scan" in plan_names(r)
        assert "Residual Filter" in plan_names(r)
        assert sorted(row[0] for row in r["rows"]) == list(range(10, 21))

    def test_igualdad_index_scan_y_rango_residual(self, engine):
        tabla_heap(engine)
        # La igualdad con índice gana al rango; el rango filtra después.
        r = q(engine, "SELECT id FROM t WHERE id = 15 AND v >= 100;")
        assert "Index Scan" in plan_names(r)
        assert "Residual Filter" in plan_names(r)
        assert r["rows"] == [[15]]
        # La residual descarta la fila que sí cumple la igualdad.
        r = q(engine, "SELECT id FROM t WHERE id = 5 AND v >= 100;")
        assert r["rows"] == []

    def test_and_con_between(self, engine):
        tabla_heap(engine)
        r = q(engine, "SELECT id FROM t WHERE v BETWEEN 100 AND 200 "
                      "AND w = 0;")
        assert "Index Range Scan" in plan_names(r)
        assert "Residual Filter" in plan_names(r)
        assert sorted(row[0] for row in r["rows"]) == [12, 15, 18]

    def test_between_como_residual(self, engine):
        tabla_heap(engine)
        r = q(engine, "SELECT id FROM t WHERE w = 1 AND v BETWEEN 0 AND 50;")
        assert sorted(row[0] for row in r["rows"]) == [1, 4]

    def test_limit_con_filtro_residual(self, engine):
        tabla_heap(engine)
        r = q(engine, "SELECT id FROM t WHERE v >= 0 AND w = 2 LIMIT 3;")
        assert [row[0] for row in r["rows"]] == [2, 5, 8]


class TestAndSinIndice:
    def test_seq_scan_con_filtro(self, engine):
        q(engine, "CREATE TABLE s (a INT, b INT);")
        for k in range(20):
            q(engine, f"INSERT INTO s VALUES ({k}, {k % 4});")
        r = q(engine, "SELECT a FROM s WHERE a >= 5 AND b = 1;")
        assert "Sequential Scan" in plan_names(r)
        assert sorted(row[0] for row in r["rows"]) == [5, 9, 13, 17]

    def test_and_sin_resultados(self, engine):
        q(engine, "CREATE TABLE s (a INT, b INT);")
        q(engine, "INSERT INTO s VALUES (1, 2);")
        r = q(engine, "SELECT a FROM s WHERE a = 1 AND b = 3;")
        assert r["rows"] == []


class TestAndSequential:
    def test_ejemplo_del_enunciado(self, engine):
        # SELECT * FROM empleados WHERE id >= 100 AND id <= 500;
        q(engine, "CREATE TABLE empleados (id INT PRIMARY KEY, "
                  "nombre VARCHAR(20)) USING SEQUENTIAL;")
        for k in range(1000):
            q(engine, f"INSERT INTO empleados VALUES ({k}, 'emp-{k}');")
        r = q(engine, "SELECT id FROM empleados "
                      "WHERE id >= 100 AND id <= 500;")
        assert "Binary Search (Sequential)" in plan_names(r)
        assert "Residual Filter" in plan_names(r)
        # Sin LIMIT aplica el tope de seguridad de 100 filas.
        assert r["truncated"]
        assert [row[0] for row in r["rows"]] == list(range(100, 200))
        # Con LIMIT explícito se obtiene el rango completo.
        r = q(engine, "SELECT id FROM empleados "
                      "WHERE id >= 100 AND id <= 500 LIMIT 500;")
        assert [row[0] for row in r["rows"]] == list(range(100, 501))


class TestAndEspacial:
    def test_radio_rtree_con_residual(self, engine):
        q(engine, "CREATE TABLE rest (nombre VARCHAR(20), ubicacion POINT);")
        q(engine, "CREATE INDEX idx_ubi ON rest (ubicacion) USING RTREE;")
        datos = [("A", (0.0, 0.0)), ("B", (1.0, 0.0)), ("C", (50.0, 50.0))]
        for n, (x, y) in datos:
            q(engine, f"INSERT INTO rest VALUES ('{n}', ({x}, {y}));")
        r = q(engine, "SELECT nombre FROM rest WHERE ubicacion "
                      "IN ((0.0, 0.0), 2.0) AND nombre = 'B';")
        assert "R-Tree Radius Search" in plan_names(r)
        assert "Residual Filter" in plan_names(r)
        assert r["rows"] == [["B"]]
