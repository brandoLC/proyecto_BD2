"""Paginación de resultados: OFFSET, pushdown de LIMIT+OFFSET y el tope
de seguridad SELECT_ROW_CAP (respuesta ``truncated``)."""

import pytest

from app.engine.executor import SELECT_ROW_CAP, Engine


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


def q(engine, sql):
    return engine.execute(sql)


def fetch_detail(result):
    return next(s["detail"] for s in result["plan"]
                if s["name"] == "Fetch Rows")


def crear_tabla_seq(engine, nombre, n):
    """Tabla con n filas: PK explícita 1..n y valor derivado del id."""
    q(engine, f"CREATE TABLE {nombre} (id INT PRIMARY KEY, v INT);")
    for i in range(1, n + 1):
        assert q(engine, f"INSERT INTO {nombre} VALUES ({i}, {i * 10});")["ok"]


class TestLimitOffset:
    """Semántica SQL estándar: saltar las primeras `offset` filas del
    resultado y después aplicar el límite."""

    def test_limit_offset_devuelve_ventana_correcta(self, engine):
        crear_tabla_seq(engine, "t", 50)
        full = q(engine, "SELECT * FROM t;")
        assert full["ok"] and full["rowcount"] == 50
        r = q(engine, "SELECT * FROM t LIMIT 5 OFFSET 10;")
        assert r["ok"] and r["rowcount"] == 5
        # exactamente las filas 11-15 del full scan (ids 11..15)
        assert r["rows"] == full["rows"][10:15]
        assert [row[0] for row in r["rows"]] == [11, 12, 13, 14, 15]

    def test_offset_cero_equivale_a_limit_solo(self, engine):
        crear_tabla_seq(engine, "t", 20)
        r = q(engine, "SELECT * FROM t LIMIT 3 OFFSET 0;")
        r2 = q(engine, "SELECT * FROM t LIMIT 3;")
        assert r["rows"] == r2["rows"]
        assert [row[0] for row in r["rows"]] == [1, 2, 3]

    def test_offset_mas_alla_del_final_devuelve_vacio(self, engine):
        crear_tabla_seq(engine, "t", 10)
        r = q(engine, "SELECT * FROM t LIMIT 5 OFFSET 10;")
        assert r["ok"] and r["rows"] == [] and r["rowcount"] == 0

    def test_limit_offset_con_where(self, engine):
        crear_tabla_seq(engine, "t", 50)
        # pares: 2, 4, ..., 50; la ventana [4:9] son 12, 16, 20, 24, 28
        r = q(engine, "SELECT id FROM t WHERE v >= 0 LIMIT 3 OFFSET 4;")
        full = q(engine, "SELECT id FROM t WHERE v >= 0;")
        assert r["rows"] == full["rows"][4:7]

    def test_offset_sin_limit_es_error_de_parseo(self, engine):
        # La gramática exige LIMIT antes de OFFSET (no se soporta la
        # forma `OFFSET ... LIMIT`): `OFFSET` solo se acepta como
        # continuación de `LIMIT n`.
        r = q(engine, "SELECT * FROM t OFFSET 10;")
        assert not r["ok"] and r["stage"] == "parse"
        assert "OFFSET" in r["error"]


class TestPushdownLimitOffset:
    """El escaneo se detiene tras juntar offset+limit filas que cumplen,
    no recorre toda la tabla."""

    def test_pushdown_sin_where(self, engine):
        crear_tabla_seq(engine, "t", 50)
        r = q(engine, "SELECT * FROM t LIMIT 3 OFFSET 4;")
        assert r["ok"] and r["rowcount"] == 3
        # leyó exactamente 7 registros (4 saltados + 3 devueltos), no 50
        assert fetch_detail(r) == "7 registros leídos del heap"
        assert [row[0] for row in r["rows"]] == [5, 6, 7]

    def test_pushdown_con_where_seq_scan(self, engine):
        crear_tabla_seq(engine, "t", 50)
        # WHERE sobre columna sin índice que coincide con todas las filas
        r = q(engine, "SELECT * FROM t WHERE v >= 0 LIMIT 3 OFFSET 4;")
        assert r["ok"] and r["rowcount"] == 3
        assert fetch_detail(r) == "7 registros leídos del heap"

    def test_pushdown_index_scan(self, engine):
        crear_tabla_seq(engine, "t", 50)
        # WHERE id BETWEEN 1 AND 50 usa el BTREE de la PK: recorta RIDs
        r = q(engine, "SELECT * FROM t WHERE id BETWEEN 1 AND 50 "
                      "LIMIT 3 OFFSET 4;")
        assert r["ok"] and r["rowcount"] == 3
        assert fetch_detail(r) == "3 registros leídos del heap"
        assert [row[0] for row in r["rows"]] == [5, 6, 7]


class TestRowCapTruncated:
    """Tope de seguridad: SELECT sin LIMIT explícito devuelve máximo
    SELECT_ROW_CAP filas con ``truncated: true``; un LIMIT explícito se
    respeta completo aunque supere el tope."""

    def test_sin_limit_trunca_a_cap(self, engine):
        crear_tabla_seq(engine, "big", SELECT_ROW_CAP + 100)
        r = q(engine, "SELECT * FROM big;")
        assert r["ok"] and r["rowcount"] == SELECT_ROW_CAP
        assert len(r["rows"]) == SELECT_ROW_CAP
        assert r["truncated"] is True
        # lectura acotada: sondeó una fila extra, no las SELECT_ROW_CAP+100
        assert fetch_detail(r) == f"{SELECT_ROW_CAP + 1} registros " \
                                  f"leídos del heap"
        # son las primeras SELECT_ROW_CAP filas, en orden
        assert [row[0] for row in r["rows"]] == list(
            range(1, SELECT_ROW_CAP + 1))

    def test_tabla_exactamente_del_cap_no_trunca(self, engine):
        # El sondeo de la fila extra distingue "exactamente SELECT_ROW_CAP"
        # de "> SELECT_ROW_CAP"
        crear_tabla_seq(engine, "big", SELECT_ROW_CAP)
        r = q(engine, "SELECT * FROM big;")
        assert r["ok"] and r["rowcount"] == SELECT_ROW_CAP
        assert r["truncated"] is False

    def test_tabla_menor_al_cap_no_trunca(self, engine):
        crear_tabla_seq(engine, "t", 50)
        r = q(engine, "SELECT * FROM t;")
        assert r["ok"] and r["rowcount"] == 50
        assert r["truncated"] is False

    def test_limit_explicito_mayor_al_cap_se_respeta(self, engine):
        total = SELECT_ROW_CAP + 100
        crear_tabla_seq(engine, "big", total)
        r = q(engine, f"SELECT * FROM big LIMIT {SELECT_ROW_CAP * 2};")
        assert r["ok"] and r["rowcount"] == total
        assert len(r["rows"]) == total
        # el usuario pidió ese límite a propósito: no se marca truncated
        assert r["truncated"] is False

    def test_truncated_respeta_where(self, engine):
        crear_tabla_seq(engine, "big", SELECT_ROW_CAP + 100)
        r = q(engine, "SELECT * FROM big WHERE id <= 10;")
        assert r["ok"] and r["rowcount"] == 10
        assert r["truncated"] is False

    def test_limit_offset_sobre_tabla_grande(self, engine):
        crear_tabla_seq(engine, "big", SELECT_ROW_CAP + 100)
        # página 2 pgAdmin: las filas SELECT_ROW_CAP+1 .. +5 del full scan
        full = q(engine, f"SELECT * FROM big LIMIT {SELECT_ROW_CAP + 100};")
        r = q(engine, f"SELECT * FROM big LIMIT 5 "
                      f"OFFSET {SELECT_ROW_CAP};")
        assert r["ok"] and r["rowcount"] == 5
        assert r["rows"] == full["rows"][SELECT_ROW_CAP:SELECT_ROW_CAP + 5]
        assert [row[0] for row in r["rows"]] == [SELECT_ROW_CAP + 1,
                                                 SELECT_ROW_CAP + 2,
                                                 SELECT_ROW_CAP + 3,
                                                 SELECT_ROW_CAP + 4,
                                                 SELECT_ROW_CAP + 5]
