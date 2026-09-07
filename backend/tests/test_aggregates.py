"""Funciones de agregación del SELECT: COUNT/MIN/MAX/SUM/AVG.

Semántica: un solo grupo (agregación global, sin GROUP BY). El resultado
es siempre una fila y una columna por expresión, con nombres ``count`` y
``func_col`` (p. ej. ``min_lat``, ``avg_rating``). Con cero filas
coincidentes COUNT devuelve 0 y MIN/MAX/SUM/AVG devuelven None (NULL).
No se permite mezclar columnas sueltas con agregados.
"""

import pytest

from app.engine.executor import Engine


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


def q(engine, sql):
    return engine.execute(sql)


def plan_names(result):
    return [s["name"] for s in result["plan"]]


@pytest.fixture()
def datos(engine):
    """Tabla pequeña con valores conocidos."""
    q(engine, "CREATE TABLE rest (nombre VARCHAR(30), lat FLOAT, "
              "rating INT, ubicacion POINT);")
    filas = [("La Mar", -12.1, 4, (-12.1, -77.1)),
             ("Central", -12.15, 5, (-12.15, -77.15)),
             ("Maido", -12.05, 3, (-12.05, -77.05))]
    for n, lat, r, (x, y) in filas:
        assert q(engine, f"INSERT INTO rest VALUES ('{n}', {lat}, {r}, "
                         f"({x}, {y}));")["ok"]
    return engine


class TestCount:
    def test_count_star_sin_where(self, datos):
        r = q(datos, "SELECT COUNT(*) FROM rest;")
        assert r["ok"] and r["columns"] == ["count"]
        assert r["rows"] == [[3]] and r["rowcount"] == 1
        assert r["truncated"] is False

    def test_count_star_con_where(self, datos):
        r = q(datos, "SELECT COUNT(*) FROM rest WHERE rating >= 4;")
        assert r["rows"] == [[2]]

    def test_count_cero_filas(self, datos):
        r = q(datos, "SELECT COUNT(*) FROM rest WHERE rating > 100;")
        assert r["rows"] == [[0]]

    def test_count_case_insensitive(self, datos):
        r = q(datos, "select count(*) from rest;")
        assert r["ok"] and r["rows"] == [[3]]

    def test_count_con_limit_no_aplica_truncated(self, datos):
        r = q(datos, "SELECT COUNT(*) FROM rest LIMIT 1;")
        assert r["ok"] and r["rows"] == [[3]] and r["truncated"] is False


class TestMinMax:
    def test_min_max_float(self, datos):
        r = q(datos, "SELECT MIN(lat), MAX(lat) FROM rest;")
        assert r["columns"] == ["min_lat", "max_lat"]
        assert r["rows"] == [[-12.15, -12.05]]

    def test_min_max_varchar_lexicografico(self, datos):
        r = q(datos, "SELECT MIN(nombre), MAX(nombre) FROM rest;")
        assert r["rows"] == [["Central", "Maido"]]

    def test_min_max_cero_filas_son_null(self, datos):
        r = q(datos, "SELECT MIN(rating), MAX(rating) FROM rest "
                     "WHERE rating > 100;")
        assert r["rows"] == [[None, None]]

    def test_min_sobre_point_error_semantico(self, datos):
        r = q(datos, "SELECT MIN(ubicacion) FROM rest;")
        assert not r["ok"] and r["stage"] == "semantic"
        assert "MIN no soporta el tipo POINT" in r["error"]

    def test_max_sobre_point_error_semantico(self, datos):
        r = q(datos, "SELECT MAX(ubicacion) FROM rest;")
        assert not r["ok"] and "MAX no soporta el tipo POINT" in r["error"]


class TestSumAvg:
    def test_avg_int_devuelve_float(self, datos):
        r = q(datos, "SELECT AVG(rating) FROM rest;")  # (4+5+3)/3
        assert r["columns"] == ["avg_rating"]
        assert isinstance(r["rows"][0][0], float)
        assert r["rows"][0][0] == pytest.approx(4.0)

    def test_sum_int_devuelve_int(self, datos):
        r = q(datos, "SELECT SUM(rating) FROM rest;")
        assert r["columns"] == ["sum_rating"]
        assert r["rows"] == [[12]]
        assert isinstance(r["rows"][0][0], int)

    def test_sum_float_devuelve_float(self, datos):
        r = q(datos, "SELECT SUM(lat) FROM rest;")
        assert isinstance(r["rows"][0][0], float)

    def test_sum_avg_cero_filas_son_null(self, datos):
        r = q(datos, "SELECT SUM(rating), AVG(rating) FROM rest "
                     "WHERE rating > 100;")
        assert r["rows"] == [[None, None]]

    def test_sum_sobre_varchar_error_semantico(self, datos):
        r = q(datos, "SELECT SUM(nombre) FROM rest;")
        assert not r["ok"] and r["stage"] == "semantic"
        assert "SUM no soporta el tipo VARCHAR" in r["error"]

    def test_avg_sobre_varchar_error_semantico(self, datos):
        r = q(datos, "SELECT AVG(nombre) FROM rest;")
        assert not r["ok"] and "AVG no soporta el tipo VARCHAR" in r["error"]

    def test_avg_sobre_point_error_semantico(self, datos):
        r = q(datos, "SELECT AVG(ubicacion) FROM rest;")
        assert not r["ok"] and "AVG no soporta el tipo POINT" in r["error"]

    def test_columna_inexistente_error_semantico(self, datos):
        r = q(datos, "SELECT SUM(precio) FROM rest;")
        assert not r["ok"] and r["stage"] == "semantic"
        assert "no existe" in r["error"]


class TestMixDeAgregados:
    def test_multiples_agregados_con_where(self, datos):
        r = q(datos, "SELECT COUNT(*), MIN(lat), MAX(lat), AVG(rating) "
                     "FROM rest WHERE rating >= 3;")
        assert r["columns"] == ["count", "min_lat", "max_lat", "avg_rating"]
        assert r["rows"] == [[3, -12.15, -12.05, pytest.approx(4.0)]]

    def test_columna_suelta_con_agregado_error(self, datos):
        r = q(datos, "SELECT nombre, COUNT(*) FROM rest;")
        assert not r["ok"] and r["stage"] == "semantic"
        assert "no se pueden mezclar columnas con agregados" in r["error"]

    def test_star_con_agregado_error(self, datos):
        r = q(datos, "SELECT *, COUNT(*) FROM rest;")
        assert not r["ok"] and "mezclar" in r["error"]

    def test_agregados_solo_where_por_pk_usa_indice(self, datos):
        r = q(datos, "SELECT COUNT(*), SUM(rating) FROM rest WHERE id = 2;")
        assert r["ok"] and r["rows"] == [[1, 5]]
        assert "Index Scan" in plan_names(r)
        assert "Aggregate" in plan_names(r)

    def test_aggregate_aparece_en_plan(self, datos):
        r = q(datos, "SELECT COUNT(*), AVG(rating) FROM rest;")
        agg = next(s for s in r["plan"] if s["name"] == "Aggregate")
        assert agg["detail"] == "count(*), avg(rating)"
        assert "Fetch Rows" in plan_names(r)


class TestErroresDeParseo:
    def test_count_con_columna_error(self, datos):
        r = q(datos, "SELECT COUNT(rating) FROM rest;")
        assert not r["ok"] and r["stage"] == "parse"
        assert "COUNT solo soporta COUNT(*)" in r["error"]

    def test_parentesis_faltante(self, datos):
        r = q(datos, "SELECT COUNT(* FROM rest;")
        assert not r["ok"] and r["stage"] == "parse"
        assert "se esperaba ')'" in r["error"]

    def test_funcion_desconocida(self, datos):
        r = q(datos, "SELECT STDDEV(rating) FROM rest;")
        assert not r["ok"] and r["stage"] == "parse"
        assert "función desconocida" in r["error"]

    def test_columna_faltante_en_agregado(self, datos):
        r = q(datos, "SELECT MIN() FROM rest;")
        assert not r["ok"] and r["stage"] == "parse"
        assert "identificador" in r["error"]

    def test_select_star_clasico_sigue_igual(self, datos):
        r = q(datos, "SELECT * FROM rest;")
        assert r["ok"] and r["columns"] == ["id", "nombre", "lat", "rating",
                                            "ubicacion"]
        assert r["rowcount"] == 3
