"""Pruebas del DiskCounter de I/O y su integración con el Engine."""

import io

import pytest

from app.engine.executor import Engine
from app.storage.disk_counter import CountedFile, DiskCounter


@pytest.fixture()
def engine(tmp_path):
    return Engine(str(tmp_path / "data"))


def _bulk_load(engine, table, start, n):
    """Carga ``n`` filas (codigo, nombre) con PKs desde ``start``."""
    rows = [(i + 2, [str(start + i), f"nombre-{start + i:06d}"])
            for i in range(n)]
    stats = engine.bulk_load_rows(table, rows)
    assert stats["rows_loaded"] == n and stats["rows_rejected"] == 0


def test_counted_file_delega_y_cuenta():
    counter = DiskCounter()
    f = CountedFile(io.BytesIO(), counter)
    f.write(b"1234")
    assert f.tell() == 4
    f.seek(0)
    assert f.read(2) == b"12"
    assert counter.snapshot() == {"reads": 1, "writes": 1}
    assert f.read() == b"34"
    assert counter.reads == 2
    # Lectura al final (0 bytes) y escritura vacía no cuentan.
    assert f.read() == b""
    assert counter.reads == 2
    f.write(b"")
    assert counter.writes == 1
    # Atributos no definidos se delegan vía __getattr__.
    assert not f.closed
    counter.reset()
    assert counter.snapshot() == {"reads": 0, "writes": 0}
    f.close()
    assert f.closed


def test_seq_scan_cuenta_lecturas_y_crece_con_la_tabla(engine):
    assert engine.execute(
        "CREATE TABLE t (codigo INT PRIMARY KEY, nombre VARCHAR(80));"
    )["ok"]
    _bulk_load(engine, "t", 0, 1000)
    r1 = engine.execute("SELECT COUNT(*) FROM t;")
    assert r1["ok"] and r1["io"]["reads"] > 0
    _bulk_load(engine, "t", 1000, 1000)
    r2 = engine.execute("SELECT COUNT(*) FROM t;")
    assert r2["io"]["reads"] > r1["io"]["reads"]


def test_index_scan_lee_mucho_menos_que_seq_scan(engine):
    engine.execute(
        "CREATE TABLE t (codigo INT PRIMARY KEY, nombre VARCHAR(80));")
    _bulk_load(engine, "t", 0, 4000)
    scan = engine.execute("SELECT COUNT(*) FROM t;")
    idx = engine.execute("SELECT * FROM t WHERE codigo = 2000;")
    assert idx["ok"] and idx["rows"][0][0] == 2000
    assert "Index Scan" in [s["name"] for s in idx["plan"]]
    assert idx["io"]["reads"] > 0
    assert idx["io"]["reads"] * 5 <= scan["io"]["reads"]


def test_insert_cuenta_escrituras(engine):
    engine.execute("CREATE TABLE t (codigo INT PRIMARY KEY, v INT);")
    r = engine.execute("INSERT INTO t VALUES (1, 100);")
    assert r["ok"]
    assert r["io"]["writes"] >= 1


def test_contador_se_resetea_entre_consultas(engine):
    engine.execute("CREATE TABLE t (codigo INT PRIMARY KEY, nombre VARCHAR(80));")
    _bulk_load(engine, "t", 0, 1000)
    r1 = engine.execute("SELECT COUNT(*) FROM t;")
    r2 = engine.execute("SELECT COUNT(*) FROM t;")
    # Dos executes consecutivos no acumulan: mismo conteo, no el doble.
    assert r2["io"] == r1["io"]


def test_resultado_execute_incluye_io(engine):
    engine.execute("CREATE TABLE t (a INT PRIMARY KEY);")
    r = engine.execute("INSERT INTO t VALUES (1);")
    assert r["ok"]
    assert set(r["io"]) == {"reads", "writes"}
    assert isinstance(r["io"]["reads"], int)
    assert isinstance(r["io"]["writes"], int)
