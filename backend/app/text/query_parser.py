"""Parser de la consulta textual dentro de ``MATCH '...'``.

Soporta operadores booleanos con la precedencia usual::

    query := or_expr
    or_expr  := and_expr (OR and_expr)*
    and_expr := not_expr ((AND | <implicito>) not_expr)*
    not_expr := [NOT] (TERM | '(' or_expr ')')

Ejemplos::

    aprendizaje AND profundo AND NOT basico
    (gato OR perro) AND NOT pajaro
    machine learning          -> AND implícito entre términos

El AST usa diccionarios: ``{"term": t}``, ``{"and": [...]}``,
``{"or": [...]}``, ``{"not": expr}``. Los términos quedan sin
procesar: la normalización (stopwords/stemming) se aplica al
evaluar contra el índice.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\s*(\(|\)|[^\s()]+)")

_AND_IMPLICIT = object()  # centinela para la conjunción implícita


class MatchQueryError(ValueError):
    """Consulta MATCH mal formada."""


def _tokenize(query: str) -> list[str]:
    tokens = [m.group(1) for m in _TOKEN_RE.finditer(query)]
    if not tokens or "".join(tokens).strip() == "":
        raise MatchQueryError("la consulta MATCH está vacía")
    return tokens


def parse_match_query(query: str) -> dict:
    """Parsea el texto de una consulta MATCH a su AST booleano."""
    tokens = _tokenize(query)
    ast, pos = _parse_or(tokens, 0)
    if pos != len(tokens):
        raise MatchQueryError(
            f"paréntesis sin cerrar u operador inesperado: {tokens[pos]!r}")
    return ast


def _parse_or(tokens: list[str], pos: int) -> tuple[dict, int]:
    left, pos = _parse_and(tokens, pos)
    while pos < len(tokens) and tokens[pos].upper() == "OR":
        right, pos = _parse_and(tokens, pos + 1)
        left = ({"or": left["or"] + [right]} if "or" in left
                else {"or": [left, right]})
    return left, pos


def _parse_and(tokens: list[str], pos: int) -> tuple[dict, int]:
    left, pos = _parse_not(tokens, pos)
    while pos < len(tokens):
        tok = tokens[pos].upper()
        if tok == "AND":
            right, pos = _parse_not(tokens, pos + 1)
        elif tok in ("OR", ")"):
            break
        else:  # yuxtaposición: "machine learning" == "machine AND learning"
            right, pos = _parse_not(tokens, pos)
        left = ({"and": left["and"] + [right]} if "and" in left
                else {"and": [left, right]})
    return left, pos


def _parse_not(tokens: list[str], pos: int) -> tuple[dict, int]:
    if pos >= len(tokens):
        raise MatchQueryError("operador sin operando al final de la consulta")
    if tokens[pos].upper() == "NOT":
        expr, pos = _parse_not(tokens, pos + 1)
        return {"not": expr}, pos
    if tokens[pos] == "(":
        expr, pos = _parse_or(tokens, pos + 1)
        if pos >= len(tokens) or tokens[pos] != ")":
            raise MatchQueryError("paréntesis sin cerrar")
        return expr, pos + 1
    if tokens[pos] == ")":
        raise MatchQueryError("paréntesis de cierre inesperado")
    if tokens[pos].upper() in ("AND", "OR"):
        raise MatchQueryError(f"operador '{tokens[pos]}' sin operando previo")
    return {"term": tokens[pos]}, pos + 1
