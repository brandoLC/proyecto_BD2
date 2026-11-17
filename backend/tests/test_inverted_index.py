"""Pruebas del módulo textual: preprocesamiento, parser de consultas
booleanas, índice invertido en disco e integración SQL (MATCH,
COSINE_SCORE, ORDER BY)."""

import pytest

from app.engine.executor import Engine
from app.indexes.inverted_index import InvertedIndex
from app.text.preprocess import TextPreprocessor
from app.text.query_parser import MatchQueryError, parse_match_query


# ----------------------------------------------------------------------
# Preprocesamiento
# ----------------------------------------------------------------------
class TestPreprocesamiento:
    def test_case_folding_y_acentos(self):
        pre = TextPreprocessor("spanish")
        assert pre.tokenize("Índice INVERSO") == ["indice", "inverso"]

    def test_stopwords(self):
        pre = TextPreprocessor("spanish")
        terms = pre.terms("el aprendizaje de la máquina")
        assert "el" not in terms and "de" not in terms
        assert len(terms) == 2

    def test_stemming_espanhol(self):
        pre = TextPreprocessor("spanish")
        assert pre.terms("corriendo") == pre.terms("correr")

    def test_stemming_ingles(self):
        pre = TextPreprocessor("english")
        assert pre.terms("running") == ["run"]

    def test_idioma_invalido(self):
        with pytest.raises(ValueError):
            TextPreprocessor("klingon")


# ----------------------------------------------------------------------
# Parser de consultas booleanas del MATCH
# ----------------------------------------------------------------------
class TestMatchQueryParser:
    def test_termino_simple(self):
        assert parse_match_query("gato") == {"term": "gato"}

    def test_and_y_and_implicito(self):
        assert parse_match_query("a AND b") == parse_match_query("a b")

    def test_precedencia_not_and_or(self):
        ast = parse_match_query("a OR b AND NOT c")
        assert "or" in ast
        assert ast["or"][1] == {"and": [{"term": "b"}, {"not": {"term": "c"}}]}

    def test_parentesis(self):
        ast = parse_match_query("(a OR b) AND c")
        assert ast == {"and": [{"or": [{"term": "a"}, {"term": "b"}]},
                               {"term": "c"}]}

    def test_errores(self):
        with pytest.raises(MatchQueryError):
            parse_match_query("")
        with pytest.raises(MatchQueryError):
            parse_match_query("(a AND b")
        with pytest.raises(MatchQueryError):
            parse_match_query("AND a")


# ----------------------------------------------------------------------
# Índice invertido en disco
# ----------------------------------------------------------------------
DOCS = [
    ((1, 0), "el aprendizaje profundo usa redes neuronales"),
    ((1, 1), "bases de datos con hashing y arboles"),
    ((2, 0), "el aprendizaje automatico basico"),
    ((2, 1), "redes neuronales para aprendizaje profundo"),
]


@pytest.fixture()
def idx(tmp_path):
    pre = TextPreprocessor("spanish")
    index = InvertedIndex(str(tmp_path / "t_c"), create=True)
    for rid, text in DOCS:
        index.add_document(rid, pre.terms(text))
    index.flush()
    index.close()
    index = InvertedIndex(str(tmp_path / "t_c"))
    yield index, pre
    index.close()


class TestInvertedIndex:
    def test_persistencia_vocabulario(self, idx):
        index, _ = idx
        assert index.live_docs == 4
        assert "aprendizaj" in index.vocab
        assert index.vocab["aprendizaj"][0] == 3  # df

    def test_postings_ordenadas_y_con_tf(self, idx):
        index, _ = idx
        postings = index.postings("profund")
        doc_ids = [d for d, _tf in postings]
        assert doc_ids == sorted(doc_ids)
        assert set(doc_ids) == {0, 3}
        assert all(tf >= 1 for _d, tf in postings)

    def test_booleano_and_or_not(self, idx):
        index, pre = idx
        and_docs = index.search_boolean(
            parse_match_query("aprendizaje AND redes"), pre)
        assert set(and_docs) == {0, 3}
        or_docs = index.search_boolean(
            parse_match_query("hashing OR basico"), pre)
        assert set(or_docs) == {1, 2}
        not_docs = index.search_boolean(
            parse_match_query("aprendizaje AND NOT basico"), pre)
        assert set(not_docs) == {0, 3}

    def test_cosine_topk(self, idx):
        index, pre = idx
        top = index.cosine_topk(pre.terms("aprendizaje profundo"), 2)
        assert len(top) == 2
        assert top[0][0] == 3  # "redes neuronales para aprendizaje profundo"
        assert top[0][1] >= top[1][1] > 0.0
        assert all(0.0 < s <= 1.0 for _d, s in top)

    def test_delete_lapida(self, idx):
        index, pre = idx
        assert index.delete_document((2, 0))  # doc 2 = "aprendizaje basico"
        docs = index.search_boolean(parse_match_query("aprendizaje"), pre)
        assert set(docs) == {0, 3}
        top = index.cosine_topk(pre.terms("aprendizaje"), 5)
        assert 2 not in {d for d, _s in top}
        # La lápida sobrevive al ciclo cerrar/abrir
        index.close()
        index2 = InvertedIndex(index.base_path)
        assert index2.live_docs == 3
        index2.close()

    def test_insert_incremental_tras_flush(self, idx):
        index, pre = idx
        index.add_document((3, 0), pre.terms("aprendizaje profundo moderno"))
        index.close()
        index2 = InvertedIndex(index.base_path)
        top = index2.cosine_topk(pre.terms("aprendizaje profundo"), 3)
        assert any(d == 4 for d, _s in top)  # el nuevo doc_id es 4
        index2.close()


# ----------------------------------------------------------------------
# Integración SQL en el motor
# ----------------------------------------------------------------------
@pytest.fixture()
def engine(tmp_path):
    eng = Engine(str(tmp_path))
    eng.execute("CREATE TABLE docs (titulo VARCHAR(60), cuerpo TEXT)")
    for titulo, cuerpo in [
            ("Intro IR", "el aprendizaje profundo usa redes neuronales"),
            ("BD2", "las bases de datos indexan con arboles y hashing"),
            ("ML basico", "el aprendizaje automatico basico con regresion"),
            ("Redes", "redes neuronales para el aprendizaje profundo")]:
        eng.execute(f"INSERT INTO docs VALUES ('{titulo}', '{cuerpo}')")
    return eng


class TestEngineTextual:
    def test_create_index_inverted(self, engine):
        r = engine.execute(
            "CREATE INDEX idx_txt ON docs (cuerpo) USING INVERTED "
            "WITH (scoring = 'TFIDF_COSINE', language = 'spanish')")
        assert r["ok"], r.get("error")
        meta = engine.catalog.index_on("docs", "cuerpo", {"INVERTED"})
        assert meta["params"]["language"] == "spanish"

    def test_create_index_inverted_requiere_texto(self, engine):
        engine.execute("CREATE TABLE nums (a INT, nota TEXT)")
        r = engine.execute("CREATE INDEX i ON nums (a) USING INVERTED")
        assert not r["ok"] and r["stage"] == "semantic"

    def test_match_booleano(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        r = engine.execute(
            "SELECT id, titulo FROM docs "
            "WHERE cuerpo MATCH 'aprendizaje AND NOT basico'")
        assert r["ok"], r.get("error")
        assert {row[0] for row in r["rows"]} == {1, 4}
        assert any(s["name"] == "Inverted Index Boolean Scan"
                   for s in r["plan"])

    def test_match_booleano_or(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        r = engine.execute(
            "SELECT id FROM docs WHERE cuerpo MATCH 'hashing OR basico'")
        assert {row[0] for row in r["rows"]} == {2, 3}

    def test_cosine_score_topk(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        r = engine.execute(
            "SELECT id, titulo, COSINE_SCORE() AS relevancia FROM docs "
            "WHERE cuerpo MATCH 'aprendizaje profundo' "
            "ORDER BY relevancia DESC LIMIT 2")
        assert r["ok"], r.get("error")
        assert r["columns"] == ["id", "titulo", "relevancia"]
        assert r["rows"][0][0] == 4
        scores = [row[2] for row in r["rows"]]
        assert scores == sorted(scores, reverse=True)
        assert any(s["name"] == "Inverted Index TopK Scan" for s in r["plan"])

    def test_cosine_score_full_scan_sin_indice(self, engine):
        r = engine.execute(
            "SELECT id, COSINE_SCORE() AS s FROM docs "
            "WHERE cuerpo MATCH 'aprendizaje profundo' "
            "ORDER BY s DESC LIMIT 2")
        assert r["ok"], r.get("error")
        assert r["rows"][0][0] == 4
        assert any(s["name"] == "Full Text Scan" for s in r["plan"])

    def test_cosine_score_requiere_match(self, engine):
        r = engine.execute(
            "SELECT id, COSINE_SCORE() AS s FROM docs WHERE id = 1")
        assert not r["ok"] and r["stage"] == "semantic"
        assert "MATCH" in r["error"]

    def test_ranked_no_admite_or(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        r = engine.execute(
            "SELECT id, COSINE_SCORE() AS s FROM docs "
            "WHERE cuerpo MATCH 'aprendizaje OR hashing'")
        assert not r["ok"] and "OR" in r["error"]

    def test_match_residual_en_and(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        r = engine.execute(
            "SELECT id FROM docs "
            "WHERE cuerpo MATCH 'aprendizaje' AND id >= 4")
        assert r["ok"], r.get("error")
        assert [row[0] for row in r["rows"]] == [4]

    def test_delete_mantiene_indice(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        engine.execute("DELETE FROM docs WHERE id = 3")
        r = engine.execute(
            "SELECT id FROM docs WHERE cuerpo MATCH 'aprendizaje'")
        assert {row[0] for row in r["rows"]} == {1, 4}

    def test_insert_tras_crear_indice(self, engine):
        engine.execute("CREATE INDEX i ON docs (cuerpo) USING INVERTED")
        engine.execute(
            "INSERT INTO docs VALUES ('Nuevo', 'aprendizaje por refuerzo')")
        r = engine.execute(
            "SELECT id, titulo FROM docs WHERE cuerpo MATCH 'refuerzo'")
        assert [row[0] for row in r["rows"]] == [5]

    def test_match_en_columna_no_textual(self, engine):
        engine.execute("CREATE TABLE t2 (a INT, b TEXT)")
        r = engine.execute("SELECT * FROM t2 WHERE a MATCH 'hola'")
        assert not r["ok"] and r["stage"] == "semantic"
