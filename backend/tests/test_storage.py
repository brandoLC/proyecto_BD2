"""Pruebas de la capa de almacenamiento: SlottedPage y HeapFile."""

import os
import struct

import pytest

from app.storage.heap_file import HeapFile
from app.storage.page import PAGE_SIZE, PageFullError, SlottedPage


class TestSlottedPage:
    def test_insert_read(self):
        page = SlottedPage()
        s1 = page.insert(b"hola")
        s2 = page.insert(b"mundo cruel")
        assert page.read(s1) == b"hola"
        assert page.read(s2) == b"mundo cruel"

    def test_delete_marca_muerto_y_slot_estable(self):
        page = SlottedPage()
        s1 = page.insert(b"aaa")
        s2 = page.insert(b"bbb")
        page.delete(s1)
        with pytest.raises(KeyError):
            page.read(s1)
        assert page.read(s2) == b"bbb"  # el slot vecino no se mueve

    def test_insert_reutiliza_slot_muerto(self):
        page = SlottedPage()
        s1 = page.insert(b"aaaa")
        page.insert(b"cccc")
        page.delete(s1)
        s3 = page.insert(b"dddd")
        assert s3 == s1  # reutilizó el slot muerto

    def test_free_space_decrece(self):
        page = SlottedPage()
        inicial = page.free_space()
        page.insert(b"x" * 100)
        assert page.free_space() == inicial - 100 - 6  # registro + slot

    def test_pagina_llena(self):
        page = SlottedPage()
        with pytest.raises(PageFullError):
            while True:
                page.insert(b"y" * 500)

    def test_compact_recupera_espacio(self):
        page = SlottedPage()
        slots = [page.insert(b"z" * 300) for _ in range(3)]
        for s in slots:
            page.delete(s)
        antes = page.free_space()
        page.compact()
        assert page.free_space() > antes
        assert page.alive_count() == 0

    def test_serializacion_roundtrip(self):
        page = SlottedPage()
        s1 = page.insert(struct.pack("<i", 42))
        s2 = page.insert(b"\x00\x01\x02")
        page.delete(s1)
        data = page.to_bytes()
        assert len(data) == PAGE_SIZE
        copia = SlottedPage.from_bytes(data)
        with pytest.raises(KeyError):
            copia.read(s1)
        assert copia.read(s2) == b"\x00\x01\x02"
        assert copia.free_space() == page.free_space()


class TestHeapFile:
    def test_insert_scan(self, tmp_path):
        with HeapFile(str(tmp_path / "t.heap"), create=True) as hf:
            rids = [hf.insert(f"reg-{i}".encode()) for i in range(50)]
            assert len(rids) == 50
            vistos = dict(hf.scan())
            assert len(vistos) == 50
            assert vistos[rids[7]] == b"reg-7"

    def test_reuso_de_lista_libre(self, tmp_path):
        with HeapFile(str(tmp_path / "t.heap"), create=True) as hf:
            rids = [hf.insert(b"a" * 100) for _ in range(20)]
            pages_antes = hf.page_count
            for rid in rids[:10]:
                hf.delete(rid)
            assert hf.row_count == 10
            nuevos = [hf.insert(b"b" * 100) for _ in range(10)]
            # los nuevos registros reutilizaron los slots liberados
            assert set(nuevos) == set(rids[:10])
            assert hf.page_count == pages_antes
            assert hf.row_count == 20

    def test_append_cuando_no_hay_libres(self, tmp_path):
        with HeapFile(str(tmp_path / "t.heap"), create=True) as hf:
            hf.insert(b"x" * 3000)
            rid2 = hf.insert(b"x" * 3000)
            assert rid2[0] == 2  # no cupo en la página 1: página nueva

    def test_delete_y_read(self, tmp_path):
        with HeapFile(str(tmp_path / "t.heap"), create=True) as hf:
            rid = hf.insert(b"temporal")
            hf.delete(rid)
            with pytest.raises(KeyError):
                hf.read(rid)
            assert hf.row_count == 0

    def test_persistencia_al_reabrir(self, tmp_path):
        path = str(tmp_path / "t.heap")
        with HeapFile(path, create=True) as hf:
            rid = hf.insert(b"persistente")
            hf.insert(b"borrado")
            hf.delete((1, 1))
        with HeapFile(path) as hf:
            assert hf.read(rid) == b"persistente"
            assert hf.row_count == 1
            assert len(hf.free_list) == 1

    def test_archivo_multiplo_de_pagina(self, tmp_path):
        path = str(tmp_path / "t.heap")
        with HeapFile(path, create=True) as hf:
            for i in range(100):
                hf.insert(f"r{i}".encode())
        assert os.path.getsize(path) % PAGE_SIZE == 0
