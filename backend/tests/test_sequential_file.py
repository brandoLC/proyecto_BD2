"""Pruebas del SequentialFile: área principal ordenada + overflow."""

import random
import struct

import pytest

from app.storage.disk_counter import DiskCounter
from app.storage.page import PAGE_SIZE
from app.storage.record import (
    Column,
    TYPE_VARCHAR,
    decode_key,
    encode_key,
    key_size,
)
from app.storage.sequential_file import (
    OVF_BASE,
    SequentialFile,
    is_overflow_rid,
)

INT_ENC = lambda v: struct.pack("<i", v)        # noqa: E731
INT_DEC = lambda b: struct.unpack("<i", b)[0]   # noqa: E731


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "t.seq"), str(tmp_path / "t.ovf")


@pytest.fixture()
def seq(paths):
    sf = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC, create=True)
    yield sf
    sf.close()


def payload_of(key, n=20):
    return f"payload-{key}".encode().ljust(n, b".")


def keys_of(sf):
    return [k for _, k, _ in sf.scan()]


class TestInsercionYScan:
    def test_insert_ordenado(self, seq):
        for k in range(1, 51):
            seq.insert(k, payload_of(k))
        assert seq.row_count == 50
        assert keys_of(seq) == list(range(1, 51))

    def test_insert_desordenado_mantiene_orden(self, seq):
        orden = list(range(1, 101))
        random.Random(42).shuffle(orden)
        for k in orden:
            seq.insert(k, payload_of(k))
        assert keys_of(seq) == list(range(1, 101))

    def test_primer_registro_va_a_la_principal(self, seq):
        rid = seq.insert(10, payload_of(10))
        assert not is_overflow_rid(rid)
        rid2 = seq.insert(20, payload_of(20))
        assert is_overflow_rid(rid2)
        assert rid2[0] >= OVF_BASE

    def test_muchos_random_orden_total(self, seq):
        orden = list(range(500))
        random.Random(7).shuffle(orden)
        for k in orden:
            seq.insert(k, payload_of(k))
        assert seq.row_count == 500
        assert keys_of(seq) == list(range(500))
        for k in (0, 123, 499):
            assert seq.search(k) is not None


class TestBusqueda:
    def test_search_en_principal_tras_reorganize(self, seq):
        for k in range(100):
            seq.insert(k, payload_of(k))
        seq.reorganize()
        found = seq.search(42)
        assert found is not None
        rid, payload = found
        assert not is_overflow_rid(rid)
        assert payload == payload_of(42)

    def test_search_en_overflow(self, seq):
        for k in range(50):
            seq.insert(k, payload_of(k))
        seq.reorganize()
        rid = seq.insert(75, payload_of(75))  # clave nueva cae al overflow
        assert is_overflow_rid(rid)
        seq.insert(80, payload_of(80))
        found = seq.search(75)
        assert found is not None and is_overflow_rid(found[0])
        assert found[1] == payload_of(75)

    def test_search_ausente(self, seq):
        for k in range(0, 100, 2):
            seq.insert(k, payload_of(k))
        assert seq.search(43) is None

    def test_search_menor_y_mayor_que_todas(self, seq):
        for k in range(10, 20):
            seq.insert(k, payload_of(k))
        assert seq.search(1) is None
        assert seq.search(999) is None

    def test_archivo_vacio(self, seq):
        assert seq.search(5) is None
        assert seq.range_search(0, 100) == []
        assert keys_of(seq) == []
        assert not seq.delete(5)


class TestRangos:
    def test_rango_cruza_principal_y_overflow(self, seq):
        for k in range(50):
            seq.insert(k, payload_of(k))
        seq.reorganize()
        # Nuevas claves intercaladas caen al overflow.
        for k in range(50, 60):
            seq.insert(k, payload_of(k))
        res = seq.range_search(45, 55)
        assert [k for _, k, _ in res] == list(range(45, 56))
        areas = {is_overflow_rid(rid) for rid, _, _ in res}
        assert areas == {False, True}  # cruza ambas áreas

    def test_rango_extremos_inclusivos_y_vacio(self, seq):
        for k in range(30):
            seq.insert(k, payload_of(k))
        assert [k for _, k, _ in seq.range_search(0, 29)] == list(range(30))
        assert seq.range_search(50, 60) == []
        assert seq.range_search(10, 5) == []


class TestDelete:
    def test_delete_en_overflow_reenlaza(self, seq):
        for k in range(20):
            seq.insert(k, payload_of(k))
        assert is_overflow_rid(seq.search(10)[0])
        assert seq.delete(10)
        assert seq.search(10) is None
        assert keys_of(seq) == [k for k in range(20) if k != 10]
        assert seq.row_count == 19

    def test_delete_en_principal(self, seq):
        for k in range(60):
            seq.insert(k, payload_of(k))
        seq.reorganize()
        rid, _ = seq.search(30)
        assert not is_overflow_rid(rid)
        assert seq.delete(30)
        assert seq.search(30) is None
        assert keys_of(seq) == [k for k in range(60) if k != 30]

    def test_delete_primero_y_ultimo(self, seq):
        for k in range(10):
            seq.insert(k, payload_of(k))
        assert seq.delete(0)
        assert seq.delete(9)
        assert keys_of(seq) == list(range(1, 9))

    def test_reinsercion_tras_delete(self, seq):
        for k in range(15):
            seq.insert(k, payload_of(k))
        seq.delete(7)
        seq.delete(8)
        seq.insert(7, payload_of(7))
        seq.insert(8, payload_of(8))
        assert keys_of(seq) == list(range(15))
        # La free list del overflow reutilizó al menos un slot muerto.
        assert seq.ovf_row_count == 14

    def test_delete_todo_resetea_areas(self, seq):
        for k in range(10):
            seq.insert(k, payload_of(k))
        for k in range(10):
            assert seq.delete(k)
        assert seq.row_count == 0
        assert seq.page_count == 1 and seq.ovf_page_count == 1
        rid = seq.insert(100, payload_of(100))
        assert not is_overflow_rid(rid)
        assert seq.search(100) is not None


class TestReorganize:
    def test_reorganize_preserva_orden_y_vacia_ovf(self, seq):
        orden = list(range(200))
        random.Random(3).shuffle(orden)
        for k in orden:
            seq.insert(k, payload_of(k))
        stats = seq.reorganize()
        assert keys_of(seq) == list(range(200))
        assert seq.ovf_page_count == 1
        assert seq.ovf_row_count == 0
        assert stats["rows_moved"] == 200
        assert stats["pages_before"] == 2  # cabecera + 1 página (el 0 inicial)
        assert stats["pages_after"] > 1
        assert stats["ovf_pages_before"] > 1
        assert stats["ovf_pages_after"] == 1
        for k in (0, 77, 199):
            assert seq.search(k) is not None

    def test_reorganize_purga_muertos(self, seq):
        for k in range(100):
            seq.insert(k, payload_of(k))
        for k in range(0, 100, 2):
            seq.delete(k)
        stats = seq.reorganize()
        assert stats["dead_purged"] == 50
        assert stats["rows_moved"] == 50
        assert keys_of(seq) == list(range(1, 100, 2))

    def test_reorganize_fill_factor(self, seq):
        for k in range(300):
            seq.insert(k, payload_of(k))
        seq.reorganize(0.75)
        for pid in range(1, seq.page_count - 1):  # salvo cabecera y última
            page = seq._read_seq_page(pid)
            used = PAGE_SIZE - page.free_space()
            assert 0.70 * PAGE_SIZE <= used <= 0.80 * PAGE_SIZE

    def test_reorganize_fill_factor_fuera_de_rango(self, seq):
        with pytest.raises(ValueError):
            seq.reorganize(0.5)
        with pytest.raises(ValueError):
            seq.reorganize(0.9)

    def test_inserciones_despues_de_reorganize(self, seq):
        for k in range(100):
            seq.insert(k, payload_of(k))
        seq.reorganize()
        for k in range(100, 150):
            seq.insert(k, payload_of(k))
        assert keys_of(seq) == list(range(150))
        stats = seq.reorganize()
        assert stats["rows_moved"] == 150


class TestPersistencia:
    def test_cerrar_y_reabrir(self, paths):
        sf = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                            create=True)
        orden = list(range(80))
        random.Random(1).shuffle(orden)
        for k in orden:
            sf.insert(k, payload_of(k))
        sf.delete(5)
        sf.close()

        sf2 = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC)
        assert sf2.row_count == 79
        assert keys_of(sf2) == [k for k in range(80) if k != 5]
        assert sf2.search(40)[1] == payload_of(40)
        assert sf2.delete(40)
        sf2.insert(200, payload_of(200))
        sf2.close()

        sf3 = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC)
        assert keys_of(sf3) == [k for k in range(80) if k not in (5, 40)] + [200]
        sf3.close()

    def test_key_size_incompatible(self, paths):
        SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                       create=True).close()
        with pytest.raises(ValueError):
            SequentialFile(paths[0], paths[1], 8, INT_ENC, INT_DEC)


class TestClavesVarchar:
    def test_varchar_orden_y_busqueda(self, tmp_path):
        col = Column("codigo", TYPE_VARCHAR, 12)
        sf = SequentialFile(
            str(tmp_path / "v.seq"), str(tmp_path / "v.ovf"),
            key_size(col),
            lambda v: encode_key(v, col),
            lambda b: decode_key(b, col),
            create=True,
        )
        nombres = ["PER", "ABC", "MNO", "AAA", "ZZZ", "DEF", "ABC2"]
        for n in nombres:
            sf.insert(n, f"datos-de-{n}".encode())
        assert keys_of(sf) == sorted(nombres)
        rid, payload = sf.search("MNO")
        assert payload == b"datos-de-MNO"
        assert sf.search("QQQ") is None
        assert [k for _, k, _ in sf.range_search("ABC", "MNO")] == \
            ["ABC", "ABC2", "DEF", "MNO"]
        assert sf.delete("AAA")
        assert keys_of(sf) == sorted(n for n in nombres if n != "AAA")
        sf.close()


class TestCounter:
    def test_counter_cuenta_reads_y_writes(self, paths):
        counter = DiskCounter()
        sf = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                            create=True, counter=counter)
        for k in range(50):
            sf.insert(k, payload_of(k))
        sf.reorganize()
        sf.close()
        assert counter.writes > 0

        c2 = DiskCounter()
        sf2 = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                             counter=c2)
        base = c2.reads  # cabeceras leídas al abrir
        assert sf2.search(25) is not None
        assert c2.reads > base
        sf2.close()

    def test_sin_counter_comportamiento_identico(self, paths):
        sf = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                            create=True, counter=None)
        for k in range(20):
            sf.insert(k, payload_of(k))
        assert keys_of(sf) == list(range(20))
        sf.close()


class TestDefer:
    def test_defer_modes_carga_masiva(self, paths):
        sf = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC,
                            create=True)
        sf.defer_header = True
        sf.defer_flush = True
        for k in range(300):
            sf.insert(k, payload_of(k))
        sf.close()  # vuelca cabeceras y páginas pendientes
        sf2 = SequentialFile(paths[0], paths[1], 4, INT_ENC, INT_DEC)
        assert sf2.row_count == 300
        assert keys_of(sf2) == list(range(300))
        sf2.close()
