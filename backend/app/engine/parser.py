"""Tokenizer + parser recursivo descendente del subconjunto SQL de MiniDB.

Gramática soportada (keywords case-insensitive, ';' final opcional)::

    statement   := create_table | create_index | insert | select | delete
                 | load_file | drop_table
    create_table:= CREATE TABLE ident '(' col_def (',' col_def)* ')'
                 | CREATE TABLE ident FROM FILE STRING
    col_def     := ident type [PRIMARY KEY]
    type        := INT | FLOAT | BOOL | TEXT | POINT | VARCHAR '(' num ')'
    create_index:= CREATE INDEX [ident] ON ident '(' ident ')'
                   USING (BTREE | HASH | RTREE)
    insert      := INSERT INTO ident VALUES '(' value (',' value)* ')'
    value       := ['-'] NUMBER | STRING | TRUE | FALSE | '(' num ',' num ')'
    select      := SELECT ('*' | ident (',' ident)*) FROM ident
                   [WHERE cond] [LIMIT num]
    cond        := ident op literal                       (op: = < <= > >=)
                 | ident BETWEEN literal AND literal
                 | ident IN '(' point ',' num ')'         (radio espacial)
                 | ident KNN '(' point ',' num ')'        (k vecinos)
    delete      := DELETE FROM ident WHERE ident '=' literal
    load_file   := LOAD INTO ident FROM FILE STRING
    drop_table  := DROP TABLE ident

El AST se representa con diccionarios. Los errores de sintaxis llevan la
posición (offset en caracteres) dentro del mensaje.
"""

from __future__ import annotations

import re

KEYWORDS = {
    "CREATE", "TABLE", "PRIMARY", "KEY", "INT", "FLOAT", "VARCHAR", "TEXT",
    "BOOL", "POINT", "INDEX", "ON", "USING", "BTREE", "HASH", "RTREE",
    "INSERT", "INTO", "VALUES", "SELECT", "FROM", "WHERE", "LIMIT",
    "BETWEEN", "AND", "IN", "KNN", "DELETE", "TRUE", "FALSE",
    "FILE", "LOAD", "DROP",
}

TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+\.\d+|\d+)
  | (?P<string>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<op><=|>=|<>|!=|=|<|>)
  | (?P<punct>[(),;*\-])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<bad>.)
    """,
    re.VERBOSE,
)


class ParseError(Exception):
    """Error de sintaxis con información de posición."""

    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"{message} (posición {pos})")
        self.pos = pos


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: str, pos: int) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - depuración
        return f"Token({self.kind}, {self.value!r}, {self.pos})"


def tokenize(sql: str) -> list[Token]:
    """Convierte el texto SQL en una lista de tokens con posición."""
    tokens: list[Token] = []
    for m in TOKEN_RE.finditer(sql):
        kind = m.lastgroup
        text = m.group()
        if kind == "ws":
            continue
        if kind == "bad":
            raise ParseError(f"carácter inesperado {text!r}", m.start())
        if kind == "word" and text.upper() in KEYWORDS:
            tokens.append(Token("kw", text.upper(), m.start()))
        else:
            tokens.append(Token(kind, text, m.start()))
    tokens.append(Token("eof", "", len(sql)))
    return tokens


class Parser:
    """Parser recursivo descendente: produce el AST de la sentencia."""

    def __init__(self, sql: str) -> None:
        self.tokens = tokenize(sql)
        self.i = 0

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def peek(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_kw(self, *kws: str) -> Token:
        tok = self.peek()
        if tok.kind == "kw" and tok.value in kws:
            return self.advance()
        esperado = " o ".join(kws)
        raise ParseError(f"se esperaba {esperado}, se encontró {tok.value!r}",
                         tok.pos)

    def accept_kw(self, kw: str) -> bool:
        tok = self.peek()
        if tok.kind == "kw" and tok.value == kw:
            self.advance()
            return True
        return False

    def expect_punct(self, p: str) -> Token:
        tok = self.peek()
        if tok.kind == "punct" and tok.value == p:
            return self.advance()
        raise ParseError(f"se esperaba '{p}', se encontró {tok.value!r}",
                         tok.pos)

    def expect_ident(self) -> str:
        tok = self.peek()
        # Los tipos/keywords de índice pueden usarse como identificadores
        # solo si no son keywords reservados estructurales.
        if tok.kind == "word":
            return self.advance().value.lower()
        raise ParseError(f"se esperaba un identificador, se encontró "
                         f"{tok.value!r}", tok.pos)

    def expect_number(self) -> int | float:
        tok = self.peek()
        if tok.kind != "number":
            raise ParseError(f"se esperaba un número, se encontró "
                             f"{tok.value!r}", tok.pos)
        self.advance()
        return float(tok.value) if "." in tok.value else int(tok.value)

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------
    def parse(self) -> dict:
        tok = self.peek()
        if tok.kind != "kw":
            raise ParseError("se esperaba una sentencia SQL", tok.pos)
        if tok.value == "CREATE":
            stmt = self._parse_create()
        elif tok.value == "INSERT":
            stmt = self._parse_insert()
        elif tok.value == "SELECT":
            stmt = self._parse_select()
        elif tok.value == "DELETE":
            stmt = self._parse_delete()
        elif tok.value == "LOAD":
            stmt = self._parse_load()
        elif tok.value == "DROP":
            stmt = self._parse_drop()
        else:
            raise ParseError(f"sentencia no soportada: {tok.value}", tok.pos)
        tok = self.peek()
        if tok.kind == "punct" and tok.value == ";":
            self.advance()
        if self.peek().kind != "eof":
            raise ParseError("contenido inesperado al final de la sentencia",
                             self.peek().pos)
        return stmt

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------
    def _parse_create(self) -> dict:
        self.expect_kw("CREATE")
        if self.accept_kw("TABLE"):
            return self._parse_create_table()
        if self.accept_kw("INDEX"):
            return self._parse_create_index()
        raise ParseError("se esperaba TABLE o INDEX después de CREATE",
                         self.peek().pos)

    def _parse_create_table(self) -> dict:
        name = self.expect_ident()
        if self.accept_kw("FROM"):
            self.expect_kw("FILE")
            return {"type": "create_table_from_file", "table": name,
                    "file": self._parse_string()}
        self.expect_punct("(")
        columns = [self._parse_col_def()]
        while self.peek().kind == "punct" and self.peek().value == ",":
            self.advance()
            columns.append(self._parse_col_def())
        self.expect_punct(")")
        pk = [c["name"] for c in columns if c["primary_key"]]
        if len(pk) > 1:
            raise ParseError("solo se permite una PRIMARY KEY por tabla",
                             self.peek().pos)
        return {"type": "create_table", "table": name, "columns": columns}

    def _parse_col_def(self) -> dict:
        name = self.expect_ident()
        tok = self.expect_kw("INT", "FLOAT", "BOOL", "TEXT", "POINT", "VARCHAR")
        col_type = tok.value
        size = None
        if col_type == "VARCHAR":
            self.expect_punct("(")
            n = self.expect_number()
            if not isinstance(n, int) or n <= 0:
                raise ParseError("VARCHAR requiere un tamaño entero positivo",
                                 self.peek().pos)
            size = n
            self.expect_punct(")")
        primary_key = False
        if self.accept_kw("PRIMARY"):
            self.expect_kw("KEY")
            primary_key = True
        return {"name": name, "type": col_type, "size": size,
                "primary_key": primary_key}

    def _parse_create_index(self) -> dict:
        # nombre de índice opcional: si el siguiente token es ON, no hay nombre
        idx_name = None
        if not (self.peek().kind == "kw" and self.peek().value == "ON"):
            idx_name = self.expect_ident()
        self.expect_kw("ON")
        table = self.expect_ident()
        self.expect_punct("(")
        column = self.expect_ident()
        self.expect_punct(")")
        self.expect_kw("USING")
        tok = self.expect_kw("BTREE", "HASH", "RTREE")
        return {"type": "create_index", "name": idx_name, "table": table,
                "column": column, "index_type": tok.value}

    # ------------------------------------------------------------------
    # INSERT
    # ------------------------------------------------------------------
    def _parse_insert(self) -> dict:
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.expect_ident()
        self.expect_kw("VALUES")
        self.expect_punct("(")
        values = [self._parse_value()]
        while self.peek().kind == "punct" and self.peek().value == ",":
            self.advance()
            values.append(self._parse_value())
        self.expect_punct(")")
        return {"type": "insert", "table": table, "values": values}

    def _parse_value(self):
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "(":
            return self._parse_point()
        if tok.kind == "number":
            return self.expect_number()
        if tok.kind == "string":
            return self._parse_string()
        if tok.kind == "kw" and tok.value in ("TRUE", "FALSE"):
            self.advance()
            return tok.value == "TRUE"
        if tok.value == "-":
            self.advance()
            n = self.expect_number()
            return -n
        raise ParseError(f"valor inesperado {tok.value!r}", tok.pos)

    def _parse_point(self) -> tuple[float, float]:
        """Literal de punto: '(' num ',' num ')'. Devuelve ``(x, y)``."""
        self.expect_punct("(")
        x = self._parse_signed_number()
        self.expect_punct(",")
        y = self._parse_signed_number()
        self.expect_punct(")")
        return (float(x), float(y))

    def _parse_string(self) -> str:
        """Literal de texto con comillas simples o dobles (se duplican
        para escaparlas)."""
        tok = self.peek()
        if tok.kind != "string":
            raise ParseError(f"se esperaba un literal de texto, se "
                             f"encontró {tok.value!r}", tok.pos)
        self.advance()
        q = tok.value[0]
        return tok.value[1:-1].replace(q + q, q)

    def _parse_signed_number(self) -> int | float:
        tok = self.peek()
        if tok.value == "-":
            self.advance()
            return -self.expect_number()
        return self.expect_number()

    # ------------------------------------------------------------------
    # SELECT
    # ------------------------------------------------------------------
    def _parse_select(self) -> dict:
        self.expect_kw("SELECT")
        columns: list[str] = []
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "*":
            self.advance()
            columns = ["*"]
        else:
            columns.append(self.expect_ident())
            while self.peek().kind == "punct" and self.peek().value == ",":
                self.advance()
                columns.append(self.expect_ident())
        self.expect_kw("FROM")
        table = self.expect_ident()
        where = None
        if self.accept_kw("WHERE"):
            where = self._parse_condition()
        limit = None
        if self.accept_kw("LIMIT"):
            n = self.expect_number()
            if not isinstance(n, int) or n < 0:
                raise ParseError("LIMIT requiere un entero no negativo",
                                 self.peek().pos)
            limit = n
        return {"type": "select", "table": table, "columns": columns,
                "where": where, "limit": limit}

    def _parse_condition(self) -> dict:
        column = self.expect_ident()
        tok = self.peek()
        if tok.kind == "kw" and tok.value == "BETWEEN":
            self.advance()
            low = self._parse_value()
            self.expect_kw("AND")
            high = self._parse_value()
            return {"kind": "between", "column": column,
                    "low": low, "high": high}
        if tok.kind == "kw" and tok.value == "IN":
            self.advance()
            self.expect_punct("(")
            center = self._parse_point()
            self.expect_punct(",")
            radius = float(self._parse_signed_number())
            self.expect_punct(")")
            return {"kind": "radius", "column": column,
                    "center": center, "radius": radius}
        if tok.kind == "kw" and tok.value == "KNN":
            self.advance()
            self.expect_punct("(")
            center = self._parse_point()
            self.expect_punct(",")
            k = self._parse_signed_number()
            self.expect_punct(")")
            if not isinstance(k, int) or k <= 0:
                raise ParseError("KNN requiere un entero positivo", tok.pos)
            return {"kind": "knn", "column": column, "center": center, "k": k}
        if tok.kind == "op":
            self.advance()
            if tok.value in ("<>", "!="):
                raise ParseError("operador no soportado en WHERE", tok.pos)
            value = self._parse_value()
            return {"kind": "compare", "column": column,
                    "op": tok.value, "value": value}
        raise ParseError("condición WHERE no soportada", tok.pos)

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------
    def _parse_delete(self) -> dict:
        self.expect_kw("DELETE")
        self.expect_kw("FROM")
        table = self.expect_ident()
        self.expect_kw("WHERE")
        column = self.expect_ident()
        tok = self.peek()
        if tok.kind == "op" and tok.value == "=":
            self.advance()
        else:
            raise ParseError("DELETE solo soporta condiciones de igualdad",
                             tok.pos)
        value = self._parse_value()
        return {"type": "delete", "table": table, "column": column,
                "value": value}

    # ------------------------------------------------------------------
    # LOAD INTO ... FROM FILE
    # ------------------------------------------------------------------
    def _parse_load(self) -> dict:
        self.expect_kw("LOAD")
        self.expect_kw("INTO")
        table = self.expect_ident()
        self.expect_kw("FROM")
        self.expect_kw("FILE")
        return {"type": "load_file", "table": table,
                "file": self._parse_string()}

    # ------------------------------------------------------------------
    # DROP TABLE
    # ------------------------------------------------------------------
    def _parse_drop(self) -> dict:
        self.expect_kw("DROP")
        self.expect_kw("TABLE")
        return {"type": "drop_table", "table": self.expect_ident()}


def parse(sql: str) -> dict:
    """Parsea una sentencia SQL y devuelve su AST."""
    return Parser(sql).parse()
