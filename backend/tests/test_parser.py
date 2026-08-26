"""Pruebas del parser: aceptación, rechazo y detalles del AST."""

import pytest

from app.engine.parser import ParseError, parse


class TestAceptacion:
    def test_create_table_todos_los_tipos(self):
        ast = parse(
            "CREATE TABLE t (id INT PRIMARY KEY, x FLOAT, n VARCHAR(20), "
            "d TEXT, b BOOL, p POINT);"
        )
        assert ast["type"] == "create_table"
        assert ast["table"] == "t"
        tipos = {c["name"]: c["type"] for c in ast["columns"]}
        assert tipos == {"id": "INT", "x": "FLOAT", "n": "VARCHAR",
                         "d": "TEXT", "b": "BOOL", "p": "POINT"}
        varchar = next(c for c in ast["columns"] if c["name"] == "n")
        assert varchar["size"] == 20
        assert ast["columns"][0]["primary_key"] is True

    def test_create_index_con_y_sin_nombre(self):
        ast = parse("CREATE INDEX idx ON t (c) USING BTREE")
        assert ast == {"type": "create_index", "name": "idx", "table": "t",
                       "column": "c", "index_type": "BTREE"}
        ast = parse("CREATE INDEX ON t (c) USING RTREE;")
        assert ast["name"] is None and ast["index_type"] == "RTREE"

    def test_insert_con_punto_y_negativos(self):
        ast = parse("INSERT INTO t VALUES (1, 'Ana', (-12.5, -77.0), TRUE);")
        assert ast["values"] == [1, "Ana", (-12.5, -77.0), True]

    def test_select_variants(self):
        assert parse("SELECT * FROM t")["columns"] == ["*"]
        ast = parse("select a, b from t where id = 5 limit 3")
        assert ast["columns"] == ["a", "b"]
        assert ast["where"] == {"kind": "compare", "column": "id",
                                "op": "=", "value": 5}
        assert ast["limit"] == 3

    def test_select_operadores(self):
        for op in ("=", "<", "<=", ">", ">="):
            ast = parse(f"SELECT * FROM t WHERE c {op} 10")
            assert ast["where"]["op"] == op

    def test_select_between(self):
        ast = parse("SELECT * FROM t WHERE c BETWEEN 1 AND 10;")
        assert ast["where"] == {"kind": "between", "column": "c",
                                "low": 1, "high": 10}

    def test_select_radius(self):
        ast = parse("SELECT * FROM t WHERE p IN ((-12.0, -77.0), 5.5)")
        assert ast["where"] == {"kind": "radius", "column": "p",
                                "center": (-12.0, -77.0), "radius": 5.5}

    def test_select_knn(self):
        ast = parse("SELECT * FROM t WHERE p KNN ((1.0, 2.0), 4)")
        assert ast["where"] == {"kind": "knn", "column": "p",
                                "center": (1.0, 2.0), "k": 4}

    def test_delete(self):
        ast = parse("DELETE FROM t WHERE id = 3;")
        assert ast == {"type": "delete", "table": "t", "column": "id",
                       "value": 3}

    def test_case_insensitive_y_nombres_en_minuscula(self):
        ast = parse("cReAtE tAbLe MiTabLa (Id InT pRiMaRy KeY)")
        assert ast["table"] == "mitabla"
        assert ast["columns"][0]["name"] == "id"

    def test_string_con_comilla_escapada(self):
        ast = parse("INSERT INTO t VALUES ('it''s')")
        assert ast["values"] == ["it's"]


class TestRechazo:
    @pytest.mark.parametrize("sql", [
        "",
        "SELECT FROM",
        "CREATE TABLE t ()",
        "SELECT * t",
        "SELECT * FROM t WHERE c <> 1",
        "INSERT INTO t VALUES",
        "DELETE FROM t",
        "SELECT * FROM t WHERE p KNN ((1,2), 0)",
        "CREATE TABLE t (a VARCHAR)",
        "SELECT * FROM t WHERE a = 1 extra",
        "DROP TABLE",
        "DROP t",
    ])
    def test_sentencias_invalidas(self, sql):
        with pytest.raises(ParseError):
            parse(sql)

    def test_error_tiene_posicion(self):
        with pytest.raises(ParseError) as excinfo:
            parse("SELECT * FROM WHERE x = 1")
        assert "posición" in str(excinfo.value)
        assert excinfo.value.pos == 14  # token 'WHERE'
