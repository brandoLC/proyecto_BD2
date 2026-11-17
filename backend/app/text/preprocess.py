"""Pipeline de preprocesamiento léxico para el índice invertido.

Etapas (siguiendo los laboratorios 8.1/8.2 del curso):

1. Case folding: todo a minúsculas.
2. Normalización Unicode: se eliminan los acentos (NFKD) conservando
   la ``ñ``; los caracteres no alfanuméricos separan tokens.
3. Eliminación de stopwords con las listas ``stoplist_es.txt`` /
   ``stoplist_en.txt`` (ambas se aplican: las colecciones del curso
   mezclan español e inglés).
4. Stemming con el algoritmo Snowball de ``nltk`` (español o inglés
   según el idioma configurado del índice).
"""

from __future__ import annotations

import os
import re
import unicodedata

from nltk.stem.snowball import SnowballStemmer

_TOKEN_RE = re.compile(r"[a-z0-9ñ]+")

LANGUAGES = ("spanish", "english", "none")


def _load_stopwords() -> frozenset[str]:
    """Unión de las stoplists en español e inglés del paquete."""
    base = os.path.dirname(__file__)
    words: set[str] = set()
    for fname in ("stoplist_es.txt", "stoplist_en.txt"):
        with open(os.path.join(base, fname), encoding="utf-8") as f:
            words.update(w.strip() for w in f if w.strip())
    return frozenset(words)


def _strip_accents(text: str) -> str:
    """Quita los diacríticos (á→a) conservando la ñ."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class _IdentityStemmer:
    """Stemmer nulo para el idioma ``none`` (tokens ya normalizados,
    p. ej. palabras visuales ``vw123`` del paradigma BoVW)."""

    def stem(self, token: str) -> str:
        return token


class TextPreprocessor:
    """Tokeniza, filtra stopwords y reduce a raíces con Snowball.

    Con ``language="none"`` no aplica stopwords ni stemming: los tokens
    se toman tal cual (índices sobre vocabularios controlados).
    """

    def __init__(self, language: str = "spanish") -> None:
        if language not in LANGUAGES:
            raise ValueError(
                f"idioma no soportado: {language!r} (usar {LANGUAGES})")
        self.language = language
        if language == "none":
            self.stopwords = frozenset()
            self.stemmer = _IdentityStemmer()
        else:
            self.stopwords = _load_stopwords()
            self.stemmer = SnowballStemmer(language)

    def tokenize(self, text: str) -> list[str]:
        """Tokens crudos: minúsculas, sin acentos, alfanuméricos."""
        return _TOKEN_RE.findall(_strip_accents(text.lower()))

    def terms(self, text: str) -> list[str]:
        """Términos normalizados (sin stopwords y con stemming).

        Conserva el orden y las repeticiones: la frecuencia de término
        ``tf`` se deriva del conteo de cada término en la lista.
        """
        return [self.stemmer.stem(tok)
                for tok in self.tokenize(text)
                if tok not in self.stopwords]
