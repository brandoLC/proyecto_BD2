"""Pruebas de la carga CSV: inferencia de esquema, upload y FROM FILE."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.engine.csv_loader import (
    cast_csv_value,
    detect_point_pair,
    derive_point_value,
    infer_columns,
    read_csv,
    sanitize_identifier,
)
from app.engine.executor import Engine
from app.engine.parser import parse
from app.storage.record import Column


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """TestClient con el engine aislado en un directorio temporal."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.api.routes as routes
    importlib.reload(routes)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app), routes.engine


def post_csv(client, url, content, filename="datos.csv", data=None):
    files = {"file": (filename, content.encode("utf-8"), "text/csv")}
    return client.post(url, files=files, data=data or {}).json()


# ----------------------------------------------------------------------
# Inferencia de esquema (lógica directa)
# ----------------------------------------------------------------------
class TestInferencia:
    def test_tipos_mixtos(self):
        header = ["id", "nombre", "rating", "activo", "ubicacion"]
        rows = [
            ["1", "La Mar", "4.8", "true", "(-12.0464, -77.0428)"],
            ["2", "Central", "5", "FALSE", "( -12.05, -77.03 )"],
            ["3", "Maido", "4.5", "true", "(-12.10, -77.05)"],
        ]
        cols = infer_columns(header, rows)
        tipos = {c.name: c.type for c in cols}
        assert tipos["id"] == "INT"
        assert tipos["nombre"] == "VARCHAR"
        assert tipos["rating"] == "FLOAT"   # mezcla "4.8" y "5" -> FLOAT
        assert tipos["activo"] == "BOOL"
        assert tipos["ubicacion"] == "POINT"

    def test_int_vs_float(self):
        cols = infer_columns(["a", "b"], [["10", "1.5"], ["-3", "2.0"]])
        assert cols[0].type == "INT"
        assert cols[1].type == "FLOAT"

    def test_bool_case_insensitive(self):
        cols = infer_columns(["ok"], [["True"], ["FALSE"], ["true"]])
        assert cols[0].type == "BOOL"
        # mezcla con otros valores ya no es BOOL
        cols = infer_columns(["ok"], [["true"], ["quizas"]])
        assert cols[0].type == "VARCHAR"

    def test_varchar_sizing(self):
        # len 25 -> 25 * 1.2 = 30 -> VARCHAR(30)
        cols = infer_columns(["s"], [["x" * 25]])
        assert cols[0].type == "VARCHAR" and cols[0].size == 30
        # mínimo 20
        cols = infer_columns(["s"], [["abc"]])
        assert cols[0].type == "VARCHAR" and cols[0].size == 20
        # más de 255 -> TEXT
        cols = infer_columns(["s"], [["x" * 300]])
        assert cols[0].type == "TEXT"

    def test_varchar_sizing_usa_todo_el_archivo(self):
        # El valor largo está FUERA del muestreo de 200 filas: el tamaño
        # debe calcularse con el máximo del archivo completo.
        rows = [["corto"]] * 250 + [["x" * 180]]
        cols = infer_columns(["s"], rows)
        # 180 * 1.2 = 216 -> VARCHAR(220); con muestreo sería VARCHAR(20)
        assert cols[0].type == "VARCHAR" and cols[0].size == 220

    def test_columna_vacia_es_text(self):
        cols = infer_columns(["a", "b"], [["1", ""], ["2", "  "]])
        assert cols[0].type == "INT"
        assert cols[1].type == "TEXT"

    def test_pk_sugerida(self):
        cols = infer_columns(["id", "v"], [["1", "a"], ["2", "b"]])
        assert cols[0].primary_key is True
        # duplicados -> sin PK
        cols = infer_columns(["id", "v"], [["1", "a"], ["1", "b"]])
        assert cols[0].primary_key is False
        # primera columna no INT -> sin PK
        cols = infer_columns(["nom", "id"], [["a", "1"], ["b", "2"]])
        assert all(not c.primary_key for c in cols)

    def test_ceros_a_la_izquierda_no_son_numeros(self):
        # "05301" es un código postal: VARCHAR, no INT (evita perder el 0)
        cols = infer_columns(["zip"], [["05301"], ["13662"]])
        assert cols[0].type == "VARCHAR"

    def test_numerico_con_violacion_tardia_degrada_a_varchar(self):
        # 250 filas de enteros limpios y un ZIP+4 al final (fuera del
        # muestreo): la columna debe degradar a VARCHAR, no rechazar filas
        rows = [[str(i)] for i in range(250)] + [["39452-6632"]]
        cols = infer_columns(["postalcode"], rows)
        assert cols[0].type == "VARCHAR"

    def test_numerico_limpio_en_todo_el_archivo_sigue_int(self):
        rows = [[str(i)] for i in range(500)]
        cols = infer_columns(["n"], rows)
        assert cols[0].type == "INT"

    def test_valores_vacios_no_descalifican(self):
        cols = infer_columns(["id"], [["1"], [""], ["2"]])
        assert cols[0].type == "INT" and cols[0].primary_key is True

    def test_identificadores_saneados(self):
        cols = infer_columns(["Código Postal", "select", "1er"], [["a", "b", "c"]])
        assert [c.name for c in cols] == ["c_digo_postal", "t_select", "t_1er"]
        assert sanitize_identifier("Mi Tabla!") == "mi_tabla_"


# ----------------------------------------------------------------------
# Detección de pares latitud/longitud (lógica directa)
# ----------------------------------------------------------------------
class TestDetectPointPair:
    def _detect(self, header, rows):
        cols = infer_columns(header, rows)
        return detect_point_pair(header, cols, rows)

    def test_aliases_latitude_longitude(self):
        det = self._detect(
            ["name", "latitude", "longitude"],
            [["A", "44.9", "-74.9"], ["B", "40.7", "-73.9"]])
        assert det == {"column": "location",
                       "lat_col": "latitude", "lng_col": "longitude"}

    def test_aliases_lat_lng(self):
        det = self._detect(["lat", "lng", "name"],
                           [["-12.04", "-77.04", "A"]])
        assert det["lat_col"] == "lat" and det["lng_col"] == "lng"

    def test_aliases_latitud_longitud(self):
        det = self._detect(
            ["latitud", "longitud"],
            [["-12.04", "-77.04"], ["-12.10", "-77.05"]])
        assert det is not None
        assert det["lat_col"] == "latitud" and det["lng_col"] == "longitud"

    def test_orden_invertido(self):
        # longitude antes que latitude: el orden no importa
        det = self._detect(["longitude", "latitude"], [["-74.9", "44.9"]])
        assert det["lat_col"] == "latitude" and det["lng_col"] == "longitude"

    def test_lon_y_int(self):
        # INT también vale si los valores están en rango
        det = self._detect(["lat", "lon"], [["44", "-74"], ["45", "-75"]])
        assert det is not None and det["lng_col"] == "lon"

    def test_fuera_de_rango_no_sugiere(self):
        # latitudes > 90: probablemente no son coordenadas
        det = self._detect(["lat", "lng"], [["150.5", "-74.9"]])
        assert det is None
        # longitud > 180
        det = self._detect(["latitude", "longitude"], [["44.9", "-190.0"]])
        assert det is None

    def test_no_float_no_sugiere(self):
        det = self._detect(["lat", "lng"], [["norte", "oeste"]])
        assert det is None

    def test_sin_par_no_sugiere(self):
        det = self._detect(["lat", "precio"], [["44.9", "10.5"]])
        assert det is None

    def test_location_existente_dedup(self):
        det = self._detect(
            ["location", "latitude", "longitude"],
            [["(1, 1)", "44.9", "-74.9"]])
        assert det["column"] == "location_2"

    def test_derive_point_value(self):
        assert derive_point_value(" 44.92 ", "-74.89", "location", 7) == \
            "(44.92, -74.89)"
        with pytest.raises(ValueError, match="lat/lng inválidos en línea 7"):
            derive_point_value("n/a", "-74.89", "location", 7)


# ----------------------------------------------------------------------
# Lectura y casteo (lógica directa)
# ----------------------------------------------------------------------
class TestLecturaYCasteo:
    def test_sniffer_punto_y_coma(self):
        header, rows = read_csv("a;b\n1;2\n3;4\n")
        assert header == ["a", "b"]
        assert rows == [(2, ["1", "2"]), (3, ["3", "4"])]

    def test_quoting_y_lineas_en_blanco(self):
        header, rows = read_csv('nom,ubi\n"La Mar, S.A.","(-12.0, -77.0)"\n\n'
                                '"Central","(-12.1, -77.1)"\n')
        assert rows[0] == (2, ["La Mar, S.A.", "(-12.0, -77.0)"])
        assert rows[1] == (4, ["Central", "(-12.1, -77.1)"])  # línea 3 vacía

    def test_csv_sin_cabecera(self):
        with pytest.raises(Exception):
            read_csv("   \n")

    def test_cast_bool(self):
        col = Column("b", "BOOL")
        assert cast_csv_value(" true ", col) is True
        assert cast_csv_value("0", col) is False
        assert cast_csv_value("1", col) is True
        with pytest.raises(ValueError):
            cast_csv_value("si", col)

    def test_cast_point_con_espacios(self):
        col = Column("p", "POINT")
        assert cast_csv_value("( -12.0464 , -77.0428 )", col) == (
            -12.0464, -77.0428)
        with pytest.raises(ValueError, match="no es POINT"):
            cast_csv_value("-12.04, -77.04", col)

    def test_cast_varchar_overflow(self):
        col = Column("s", "VARCHAR", 5)
        assert cast_csv_value("hola", col) == "hola"
        with pytest.raises(ValueError, match="excede VARCHAR"):
            cast_csv_value("demasiado largo", col)


# ----------------------------------------------------------------------
# Endpoint POST /api/infer-schema
# ----------------------------------------------------------------------
class TestInferSchemaEndpoint:
    CSV = (
        "id,nombre,rating,ubicacion\n"
        '1,La Mar,4.8,"(-12.0464, -77.0428)"\n'
        '2,Central,4.9,"(-12.05, -77.03)"\n'
        '3,Maido,4.5,"(-12.10, -77.05)"\n'
    )

    def test_infer_schema_ok(self, api):
        client, _ = api
        r = post_csv(client, "/api/infer-schema", self.CSV,
                     filename="restaurantes.csv")
        assert r["ok"] is True
        assert r["table_name"] == "restaurantes"
        tipos = {c["name"]: c["type"] for c in r["columns"]}
        assert tipos == {"id": "INT", "nombre": "VARCHAR(20)",
                         "rating": "FLOAT", "ubicacion": "POINT"}
        assert r["columns"][0]["primary_key"] is True
        assert r["suggested_sql"].startswith(
            "CREATE TABLE restaurantes (id INT PRIMARY KEY")
        assert r["preview_rows"][0][0] == "1"
        assert len(r["preview_rows"]) == 3
        assert r["total_rows_estimate"] == 3

    def test_infer_schema_table_name_explicito(self, api):
        client, _ = api
        r = post_csv(client, "/api/infer-schema", self.CSV,
                     data={"table_name": "Restaurantes Lima!"})
        assert r["ok"] and r["table_name"] == "restaurantes_lima_"

    def test_infer_schema_csv_invalido(self, api):
        client, _ = api
        r = post_csv(client, "/api/infer-schema", "  \n")
        assert r["ok"] is False and r["stage"] == "parse"

    def test_infer_schema_sin_par_latlng(self, api):
        client, _ = api
        r = post_csv(client, "/api/infer-schema", self.CSV,
                     filename="restaurantes.csv")
        assert r["ok"] is True
        assert r["derived_point"] is None
        assert r["notes"] == []
        assert not r["suggested_sql"].endswith("location POINT);")

    def test_infer_schema_con_par_latlng(self, api):
        client, _ = api
        csv_text = (
            "name,latitude,longitude,websites\n"
            "McDonald's,44.9281,-74.8919,https://mcdonalds.com\n"
            "Burger King,40.7128,-73.9352,https://bk.com\n"
        )
        r = post_csv(client, "/api/infer-schema", csv_text,
                     filename="fastfood.csv")
        assert r["ok"] is True
        assert r["derived_point"] == {"column": "location",
                                      "lat_col": "latitude",
                                      "lng_col": "longitude"}
        assert r["suggested_sql"].endswith(", location POINT);")
        # las columnas inferidas no incluyen la derivada
        assert "location" not in {c["name"] for c in r["columns"]}
        assert r["notes"] == ["columna 'location' (POINT) derivada de "
                              "'latitude' + 'longitude'"]


# ----------------------------------------------------------------------
# Endpoint POST /api/tables/{name}/upload-csv
# ----------------------------------------------------------------------
class TestUploadCSV:
    def _crear_tabla(self, engine):
        r = engine.execute(
            "CREATE TABLE rest (id INT PRIMARY KEY, nombre VARCHAR(30), "
            "rating FLOAT, ubicacion POINT);")
        assert r["ok"]

    def test_upload_orden_mezclado(self, api):
        client, engine = api
        self._crear_tabla(engine)
        csv_text = (
            "rating,nombre,id,ubicacion\n"
            '4.8,La Mar,1,"(-12.04, -77.04)"\n'
            '4.9,Central,2,"(-12.05, -77.03)"\n'
        )
        r = post_csv(client, "/api/tables/rest/upload-csv", csv_text)
        assert r["ok"] is True
        assert r["rows_loaded"] == 2 and r["rows_rejected"] == 0
        assert r["errors"] == [] and r["ignored_columns"] == []
        sel = engine.execute("SELECT id, nombre FROM rest WHERE id = 2;")
        assert sel["rows"] == [[2, "Central"]]

    def test_filas_invalidas_rechazadas_con_linea(self, api):
        client, engine = api
        self._crear_tabla(engine)
        csv_text = (
            "id,nombre,rating,ubicacion\n"
            '1,La Mar,4.8,"(-12.04, -77.04)"\n'
            'abc,Malo1,4.0,"(-12.0, -77.0)"\n'
            '2,Malo2,xyz,"(-12.0, -77.0)"\n'
            '3,Malo3,4.0,nope\n'
        )
        r = post_csv(client, "/api/tables/rest/upload-csv", csv_text)
        assert r["ok"] is True
        assert r["rows_loaded"] == 1 and r["rows_rejected"] == 3
        assert [e["line"] for e in r["errors"]] == [3, 4, 5]
        assert "no es INT" in r["errors"][0]["reason"]
        assert "no es FLOAT" in r["errors"][1]["reason"]
        assert "no es POINT" in r["errors"][2]["reason"]
        sel = engine.execute("SELECT * FROM rest;")
        assert sel["rowcount"] == 1

    def test_columna_faltante_falla_todo(self, api):
        client, engine = api
        self._crear_tabla(engine)
        r = post_csv(client, "/api/tables/rest/upload-csv",
                     "id,nombre\n1,La Mar\n")
        assert r["ok"] is False and r["stage"] == "semantic"
        assert "rating" in r["error"]

    def test_columnas_extra_ignoradas(self, api):
        client, engine = api
        self._crear_tabla(engine)
        csv_text = (
            "id,nombre,rating,ubicacion,comentario\n"
            '1,La Mar,4.8,"(-12.04, -77.04)",rico\n'
        )
        r = post_csv(client, "/api/tables/rest/upload-csv", csv_text)
        assert r["ok"] and r["rows_loaded"] == 1
        assert r["ignored_columns"] == ["comentario"]

    def test_varchar_overflow_rechazado(self, api):
        client, engine = api
        self._crear_tabla(engine)
        csv_text = (
            "id,nombre,rating,ubicacion\n"
            '1,NombreDemasiadoLargoParaVarchar30,4.8,"(-12.04, -77.04)"\n'
        )
        r = post_csv(client, "/api/tables/rest/upload-csv", csv_text)
        assert r["ok"] and r["rows_rejected"] == 1
        assert "excede VARCHAR(30)" in r["errors"][0]["reason"]

    def test_tabla_inexistente(self, api):
        client, _ = api
        r = post_csv(client, "/api/tables/nope/upload-csv", "a\n1\n")
        assert r["ok"] is False and r["stage"] == "semantic"

    def test_indice_btree_consistente_tras_carga(self, api):
        client, engine = api
        engine.execute("CREATE TABLE prod (id INT PRIMARY KEY, "
                       "precio FLOAT, nombre VARCHAR(20));")
        engine.execute("CREATE INDEX ON prod (precio) USING BTREE;")
        filas = "\n".join(f"{i},{i * 10}.5,prod{i}" for i in range(1, 51))
        r = post_csv(client, "/api/tables/prod/upload-csv",
                     "id,precio,nombre\n" + filas + "\n")
        assert r["ok"] and r["rows_loaded"] == 50
        sel = engine.execute("SELECT id FROM prod WHERE precio = 200.5;")
        assert sel["rows"] == [[20]]
        detalle = next(s["detail"] for s in sel["plan"]
                       if s["name"] == "Index Scan")
        assert "USING BTREE ON prod.precio" in detalle
        sel = engine.execute(
            "SELECT id FROM prod WHERE precio BETWEEN 100.0 AND 200.5;")
        assert len(sel["rows"]) == 11  # precios 100.5 .. 200.5


class TestUploadCSVDerivedPoint:
    CSV = (
        "id,name,latitude,longitude\n"
        "1,McDonald's,44.9281,-74.8919\n"
        "2,Burger King,40.7128,-73.9352\n"
    )

    def _crear_tabla(self, engine):
        r = engine.execute(
            "CREATE TABLE ff (id INT PRIMARY KEY, name VARCHAR(30), "
            "latitude FLOAT, longitude FLOAT, location POINT);")
        assert r["ok"]

    def test_upload_con_punto_derivado(self, api):
        client, engine = api
        self._crear_tabla(engine)
        r = post_csv(client, "/api/tables/ff/upload-csv", self.CSV,
                     data={"point_column": "location",
                           "lat_col": "latitude", "lng_col": "longitude"})
        assert r["ok"] is True
        assert r["rows_loaded"] == 2 and r["rows_rejected"] == 0
        sel = engine.execute("SELECT id, location FROM ff WHERE id = 1;")
        assert sel["rows"] == [[1, [44.9281, -74.8919]]]
        # consulta espacial sobre la columna derivada
        sel = engine.execute(
            "SELECT id FROM ff WHERE location KNN ((44.92, -74.89), 1);")
        assert sel["rows"] == [[1]]

    def test_lat_invalida_rechaza_fila(self, api):
        client, engine = api
        self._crear_tabla(engine)
        csv_text = self.CSV + "3,Roto,nope,-73.0\n"
        r = post_csv(client, "/api/tables/ff/upload-csv", csv_text,
                     data={"point_column": "location",
                           "lat_col": "latitude", "lng_col": "longitude"})
        assert r["ok"] is True
        assert r["rows_loaded"] == 2 and r["rows_rejected"] == 1
        assert r["errors"][0]["line"] == 4
        assert "location: lat/lng inválidos en línea 4" in \
            r["errors"][0]["reason"]

    def test_campos_parciales_error_semantico(self, api):
        client, engine = api
        self._crear_tabla(engine)
        for data in ({"point_column": "location"},
                     {"point_column": "location", "lat_col": "latitude"}):
            r = post_csv(client, "/api/tables/ff/upload-csv", self.CSV,
                         data=data)
            assert r["ok"] is False and r["stage"] == "semantic"
            assert "juntos" in r["error"]

    def test_point_column_inexistente(self, api):
        client, engine = api
        self._crear_tabla(engine)
        r = post_csv(client, "/api/tables/ff/upload-csv", self.CSV,
                     data={"point_column": "ubicacion",
                           "lat_col": "latitude", "lng_col": "longitude"})
        assert r["ok"] is False and r["stage"] == "semantic"
        assert "ubicacion" in r["error"]

    def test_point_column_no_es_point(self, api):
        client, engine = api
        self._crear_tabla(engine)
        r = post_csv(client, "/api/tables/ff/upload-csv", self.CSV,
                     data={"point_column": "name",
                           "lat_col": "latitude", "lng_col": "longitude"})
        assert r["ok"] is False and r["stage"] == "semantic"
        assert "no es POINT" in r["error"]

    def test_lat_col_ausente_del_csv(self, api):
        client, engine = api
        self._crear_tabla(engine)
        r = post_csv(client, "/api/tables/ff/upload-csv", self.CSV,
                     data={"point_column": "location",
                           "lat_col": "lat", "lng_col": "longitude"})
        assert r["ok"] is False and r["stage"] == "semantic"
        assert "'lat'" in r["error"]


# ----------------------------------------------------------------------
# FROM FILE (parser + executor)
# ----------------------------------------------------------------------
@pytest.fixture()
def engine_ds(tmp_path, monkeypatch):
    ds = tmp_path / "datasets"
    ds.mkdir()
    monkeypatch.setenv("DATASETS_DIR", str(ds))
    return Engine(str(tmp_path / "data")), ds


REST_CSV = (
    "id,nombre,rating,ubicacion\n"
    '1,La Mar,4.8,"(-12.0464, -77.0428)"\n'
    '2,Central,4.9,"(-12.05, -77.03)"\n'
    '3,Maido,4.5,"(-12.10, -77.05)"\n'
    "4,Roto,9.9\n"  # fila corta: se omite en la inferencia, se rechaza al cargar
)


class TestFromFile:
    def test_parse_create_from_file(self):
        ast = parse('CREATE TABLE rest FROM FILE "restaurantes.csv";')
        assert ast == {"type": "create_table_from_file", "table": "rest",
                       "file": "restaurantes.csv"}
        ast = parse("LOAD INTO rest FROM FILE 'mas.csv'")
        assert ast == {"type": "load_file", "table": "rest",
                       "file": "mas.csv"}

    def test_create_table_from_file_end_to_end(self, engine_ds):
        engine, ds = engine_ds
        (ds / "restaurantes.csv").write_text(REST_CSV, encoding="utf-8")
        r = engine.execute('CREATE TABLE rest FROM FILE "restaurantes.csv";')
        assert r["ok"] and r["kind"] == "create_table"
        assert "3 filas cargadas, 1 rechazadas" in r["message"]
        nombres = [s["name"] for s in r["plan"]]
        assert "Infer Schema" in nombres and "Bulk Load" in nombres
        assert r["load_errors"][0]["line"] == 5
        # la PK sugerida quedó en el catálogo
        pk = engine.catalog.primary_key("rest")
        assert pk is not None and pk.name == "id"
        sel = engine.execute("SELECT id, nombre FROM rest WHERE id = 2;")
        assert sel["rows"] == [[2, "Central"]]

    def test_load_into_tabla_existente(self, engine_ds):
        engine, ds = engine_ds
        (ds / "mas.csv").write_text(
            "id,nombre,rating,ubicacion\n"
            '10,Rafael,4.2,"(-12.07, -77.02)"\n'
            '11,Astrid,4.1,"(-12.08, -77.01)"\n', encoding="utf-8")
        engine.execute(
            "CREATE TABLE rest (id INT PRIMARY KEY, nombre VARCHAR(30), "
            "rating FLOAT, ubicacion POINT);")
        r = engine.execute('LOAD INTO rest FROM FILE "mas.csv";')
        assert r["ok"] and r["kind"] == "insert" and r["rowcount"] == 2
        sel = engine.execute("SELECT * FROM rest;")
        assert sel["rowcount"] == 2

    def test_load_into_tabla_inexistente(self, engine_ds):
        engine, ds = engine_ds
        (ds / "x.csv").write_text("a\n1\n", encoding="utf-8")
        r = engine.execute('LOAD INTO nope FROM FILE "x.csv";')
        assert not r["ok"] and r["stage"] == "semantic"

    def test_path_traversal_rechazado(self, engine_ds):
        engine, ds = engine_ds
        r = engine.execute('CREATE TABLE t FROM FILE "../secreto.csv";')
        assert not r["ok"] and r["stage"] == "semantic"
        r = engine.execute('LOAD INTO t FROM FILE "/etc/passwd";')
        assert not r["ok"] and r["stage"] == "semantic"

    def test_archivo_inexistente(self, engine_ds):
        engine, _ = engine_ds
        r = engine.execute('CREATE TABLE t FROM FILE "noexiste.csv";')
        assert not r["ok"] and r["stage"] == "execution"
        assert "no existe" in r["error"]

    def test_point_end_to_end_con_rtree(self, engine_ds):
        engine, ds = engine_ds
        (ds / "restaurantes.csv").write_text(REST_CSV, encoding="utf-8")
        engine.execute('CREATE TABLE rest FROM FILE "restaurantes.csv";')
        r = engine.execute("CREATE INDEX ON rest (ubicacion) USING RTREE;")
        assert r["ok"]
        sel = engine.execute(
            "SELECT id, nombre FROM rest WHERE ubicacion "
            "KNN ((-12.0464, -77.0428), 1);")
        assert sel["ok"] and sel["rows"] == [[1, "La Mar"]]
        assert "R-Tree KNN Search" in [s["name"] for s in sel["plan"]]
        sel = engine.execute(
            "SELECT id FROM rest WHERE ubicacion "
            "IN ((-12.05, -77.03), 0.02);")
        assert {row[0] for row in sel["rows"]} == {1, 2}


FF_CSV = (
    "name,latitude,longitude\n"
    "McDonald's Massena,44.9281,-74.8919\n"
    "Burger King NYC,40.7128,-73.9352\n"
    "McDonald's LA,34.0522,-118.2437\n"
)


class TestFromFileDerivedPoint:
    def test_create_from_file_deriva_point(self, engine_ds):
        engine, ds = engine_ds
        (ds / "ff.csv").write_text(FF_CSV, encoding="utf-8")
        r = engine.execute('CREATE TABLE ff FROM FILE "ff.csv";')
        assert r["ok"]
        cols = {c.name: c.type for c in engine.catalog.columns("ff")}
        assert cols["location"] == "POINT"
        assert "3 filas cargadas" in r["message"]
        detalle = next(s["detail"] for s in r["plan"]
                       if s["name"] == "Infer Schema")
        assert "location POINT derivada de latitude+longitude" in detalle
        sel = engine.execute("SELECT name, location FROM ff;")
        assert sel["rowcount"] == 3
        assert [44.9281, -74.8919] in [row[1] for row in sel["rows"]]

    def test_create_from_file_point_knn_rtree(self, engine_ds):
        engine, ds = engine_ds
        (ds / "ff.csv").write_text(FF_CSV, encoding="utf-8")
        engine.execute('CREATE TABLE ff FROM FILE "ff.csv";')
        r = engine.execute("CREATE INDEX ON ff (location) USING RTREE;")
        assert r["ok"]
        sel = engine.execute(
            "SELECT name FROM ff WHERE location "
            "KNN ((44.92, -74.89), 1);")
        assert sel["ok"] and sel["rows"] == [["McDonald's Massena"]]
        assert "R-Tree KNN Search" in [s["name"] for s in sel["plan"]]

    def test_load_into_deriva_point_automatico(self, engine_ds):
        engine, ds = engine_ds
        (ds / "ff.csv").write_text(
            "id,name,latitude,longitude\n"
            "1,McDonald's Massena,44.9281,-74.8919\n"
            "2,Burger King NYC,40.7128,-73.9352\n", encoding="utf-8")
        engine.execute(
            "CREATE TABLE ff (id INT PRIMARY KEY, name VARCHAR(30), "
            "location POINT);")
        r = engine.execute('LOAD INTO ff FROM FILE "ff.csv";')
        assert r["ok"] and r["rowcount"] == 2
        detalle = next(s["detail"] for s in r["plan"]
                       if s["name"] == "Map Columns")
        assert "location derivada de latitude+longitude" in detalle
        sel = engine.execute("SELECT id, location FROM ff WHERE id = 1;")
        assert sel["rows"] == [[1, [44.9281, -74.8919]]]

    def test_load_into_sin_par_comportamiento_actual(self, engine_ds):
        # la tabla tiene POINT pero el CSV no trae par lat/lng -> error
        engine, ds = engine_ds
        (ds / "x.csv").write_text("id,name\n1,Solo\n", encoding="utf-8")
        engine.execute(
            "CREATE TABLE ff (id INT PRIMARY KEY, name VARCHAR(30), "
            "location POINT);")
        r = engine.execute('LOAD INTO ff FROM FILE "x.csv";')
        assert not r["ok"] and r["stage"] == "semantic"
        assert "location" in r["error"]
