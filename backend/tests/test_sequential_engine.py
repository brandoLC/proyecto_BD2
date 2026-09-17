"""Pruebas de integración del SequentialFile con el motor (parser,
catálogo, executor y API)."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.engine.executor import Engine
from app.engine.parser import ParseError, parse


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """TestClient con el engine aislado en un directorio temporal."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.api.routes as routes
    importlib.reload(routes)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app), routes.engine


def q(engine, sql):
    r = engine.execute(sql)
    assert r["ok"], r.get("error")
    return r


def plan_names(result):
    return [s["name"] for s in result["plan"]]


def tabla_seq(engine, n=30):
    """Tabla SEQUENTIAL con claves 0..n-1 insertadas en desorden."""
    q(engine, "CREATE TABLE t (id INT PRIMARY KEY, nombre VARCHAR(20)) "
              "USING SEQUENTIAL;")
    for k in range(n - 1, -1, -1):
        q(engine, f"INSERT INTO t VALUES ({k}, 'nom-{k}');")


class TestCreateSequential:
    def test_create_usando_sequential(self, engine):
        r = q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v INT) "
                      "USING SEQUENTIAL;")
        assert engine.catalog.organization("t") == "sequential"
        assert "Create Sequential File" in plan_names(r)
        # La PK de una sequential NO genera B+ Tree automático.
        assert engine.catalog.indexes("t") == []

    def test_create_sin_using_sigue_heap(self, engine):
        q(engine, "CREATE TABLE t (id INT PRIMARY KEY, v INT);")
        assert engine.catalog.organization("t") == "heap"
        # Compat: la PK sí genera su B+ Tree automático en heap.
        assert engine.catalog.indexes("t")[0]["type"] == "BTREE"

    def test_using_sequential_con_pk_implicita_serial(self, engine):
        q(engine, "CREATE TABLE t (nombre VARCHAR(20)) USING SEQUENTIAL;")
        cols = engine.catalog.columns("t")
        assert cols[0].name == "id" and cols[0].auto and cols[0].primary_key
        q(engine, "INSERT INTO t VALUES ('B');")
        q(engine, "INSERT INTO t VALUES ('A');")
        r = q(engine, "SELECT * FROM t;")
        assert [row[0] for row in r["rows"]] == [1, 2]

    def test_using_keyword_invalida(self):
        with pytest.raises(ParseError):
            parse("CREATE TABLE t (a INT) USING BTREE;")
        r = parse("CREATE TABLE t (a INT) USING SEQUENTIAL;")
        assert r["organization"] == "sequential"
        assert parse("CREATE TABLE t (a INT);")["organization"] == "heap"

    def test_using_heap_explicito(self, engine):
        q(engine, "CREATE TABLE t (a INT) USING HEAP;")
        assert engine.catalog.organization("t") == "heap"


class TestSelectSequential:
    def test_inserts_desordenados_scan_ordenado(self, engine):
        tabla_seq(engine)
        r = q(engine, "SELECT * FROM t;")
        assert [row[0] for row in r["rows"]] == list(range(30))

    def test_igualdad_pk_usa_binary_search(self, engine):
        tabla_seq(engine)
        r = q(engine, "SELECT * FROM t WHERE id = 17;")
        assert r["rows"] == [[17, "nom-17"]]
        assert "Binary Search (Sequential)" in plan_names(r)
        assert q(engine, "SELECT * FROM t WHERE id = 999;")["rows"] == []

    def test_rangos_pk(self, engine):
        tabla_seq(engine)
        for sql, esperado in (
                ("SELECT * FROM t WHERE id BETWEEN 5 AND 9;", range(5, 10)),
                ("SELECT * FROM t WHERE id < 3;", range(0, 3)),
                ("SELECT * FROM t WHERE id <= 3;", range(0, 4)),
                ("SELECT * FROM t WHERE id > 26;", range(27, 30)),
                ("SELECT * FROM t WHERE id >= 26;", range(26, 30))):
            r = q(engine, sql)
            assert [row[0] for row in r["rows"]] == list(esperado)
            assert "Binary Search (Sequential)" in plan_names(r)

    def test_where_en_columna_sin_indice_hace_scan(self, engine):
        tabla_seq(engine)
        r = q(engine, "SELECT * FROM t WHERE nombre = 'nom-8';")
        assert r["rows"] == [[8, "nom-8"]]
        assert "Sequential Scan" in plan_names(r)

    def test_count_sobre_sequential(self, engine):
        tabla_seq(engine, 25)
        r = q(engine, "SELECT COUNT(*) FROM t;")
        assert r["rows"] == [[25]]


class TestInsertDeleteSequential:
    def test_pk_duplicada_rechazada(self, engine):
        tabla_seq(engine, 5)
        r = engine.execute("INSERT INTO t VALUES (3, 'otro');")
        assert not r["ok"]
        assert "clave primaria duplicada" in r["error"]

    def test_delete_por_pk(self, engine):
        tabla_seq(engine, 10)
        r = q(engine, "DELETE FROM t WHERE id = 4;")
        assert r["rowcount"] == 1
        assert q(engine, "SELECT * FROM t WHERE id = 4;")["rows"] == []
        rest = q(engine, "SELECT * FROM t;")
        assert [row[0] for row in rest["rows"]] == \
            [k for k in range(10) if k != 4]

    def test_delete_y_reinsercion(self, engine):
        tabla_seq(engine, 10)
        q(engine, "DELETE FROM t WHERE id = 5;")
        q(engine, "INSERT INTO t VALUES (5, 'cinco');")
        r = q(engine, "SELECT * FROM t WHERE id = 5;")
        assert r["rows"] == [[5, "cinco"]]


class TestIndicesSecundarios:
    def test_btree_secundario_en_sequential(self, engine):
        tabla_seq(engine)
        q(engine, "CREATE INDEX ON t (nombre) USING BTREE;")
        r = q(engine, "SELECT * FROM t WHERE nombre = 'nom-11';")
        assert r["rows"] == [[11, "nom-11"]]
        assert "Index Scan" in plan_names(r)
        # El índice se mantiene en insert y delete.
        q(engine, "INSERT INTO t VALUES (30, 'nuevo');")
        assert q(engine, "SELECT * FROM t WHERE nombre = 'nuevo';"
                 )["rows"] == [[30, "nuevo"]]
        q(engine, "DELETE FROM t WHERE id = 30;")
        assert q(engine, "SELECT * FROM t WHERE nombre = 'nuevo';"
                 )["rows"] == []

    def test_create_index_sobre_pk_sequential_rechazado(self, engine):
        tabla_seq(engine, 3)
        for itype in ("BTREE", "HASH"):
            r = engine.execute(f"CREATE INDEX ON t (id) USING {itype};")
            assert not r["ok"] and r["stage"] == "semantic"
            assert "SEQUENTIAL" in r["error"]


class TestCargaMasiva:
    def test_upload_csv_en_sequential(self, api):
        client, _eng = api
        r = client.post("/api/query", json={
            "sql": "CREATE TABLE rest (id INT PRIMARY KEY, "
                   "nombre VARCHAR(20)) USING SEQUENTIAL;"}).json()
        assert r["ok"]
        csv = "id,nombre\n3,C\n1,A\n2,B\n5,E\n4,D\n"
        files = {"file": ("datos.csv", csv.encode(), "text/csv")}
        r = client.post("/api/tables/rest/upload-csv", files=files).json()
        assert r["ok"] and r["rows_loaded"] == 3 + 2
        r = client.post("/api/query",
                        json={"sql": "SELECT * FROM rest;"}).json()
        assert [row[0] for row in r["rows"]] == [1, 2, 3, 4, 5]

    def test_bulk_load_detecta_duplicados(self, engine):
        tabla_seq(engine, 5)
        stats = engine.bulk_load_rows(
            "t", [(2, ["3", "dup"]), (3, ["9", "nueve"])])
        assert stats["rows_loaded"] == 1
        assert stats["rows_rejected"] == 1
        assert "duplicada" in stats["errors"][0]["reason"]


class TestReorganizeEndpoint:
    def test_404_tabla_inexistente(self, api):
        client, _eng = api
        r = client.post("/api/tables/nope/reorganize")
        assert r.status_code == 404

    def test_409_tabla_heap(self, api):
        client, _eng = api
        client.post("/api/query",
                    json={"sql": "CREATE TABLE h (a INT PRIMARY KEY);"})
        r = client.post("/api/tables/h/reorganize")
        assert r.status_code == 409
        assert "SEQUENTIAL" in r.json()["error"]

    def test_200_sequential_con_stats(self, api):
        client, _eng = api
        client.post("/api/query", json={
            "sql": "CREATE TABLE s (id INT PRIMARY KEY, v VARCHAR(10)) "
                   "USING SEQUENTIAL;"})
        for k in range(40):
            client.post("/api/query",
                        json={"sql": f"INSERT INTO s VALUES ({k}, 'x{k}');"})
        for k in range(0, 40, 3):
            client.post("/api/query",
                        json={"sql": f"DELETE FROM s WHERE id = {k};"})
        r = client.post("/api/tables/s/reorganize")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and body["elapsed_ms"] >= 0
        stats = body["stats"]
        assert stats["rows_moved"] == 40 - 14
        assert stats["dead_purged"] == 14
        assert stats["pages_after"] >= 1
        assert stats["ovf_pages_after"] == 1
        # Todo sigue consultable y ordenado después de reorganizar.
        r = client.post("/api/query",
                        json={"sql": "SELECT * FROM s;"}).json()
        assert [row[0] for row in r["rows"]] == \
            [k for k in range(40) if k % 3 != 0]


class TestPersistencia:
    def test_engine_nuevo_ve_la_organizacion(self, tmp_path):
        data_dir = str(tmp_path / "data")
        e1 = Engine(data_dir)
        q(e1, "CREATE TABLE t (id INT PRIMARY KEY, v INT) USING SEQUENTIAL;")
        q(e1, "INSERT INTO t VALUES (2, 20);")
        q(e1, "INSERT INTO t VALUES (1, 10);")

        e2 = Engine(data_dir)
        assert e2.catalog.organization("t") == "sequential"
        r = q(e2, "SELECT * FROM t;")
        assert r["rows"] == [[1, 10], [2, 20]]
        r = q(e2, "SELECT * FROM t WHERE id = 2;")
        assert "Binary Search (Sequential)" in plan_names(r)
        q(e2, "INSERT INTO t VALUES (3, 30);")
        assert q(e2, "SELECT * FROM t WHERE id >= 2;")["rows"] == \
            [[2, 20], [3, 30]]
        info = e2.table_info()[0]
        assert info["organization"] == "sequential"
        assert info["rowcount"] == 3
