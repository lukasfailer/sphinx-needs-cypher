"""A small, self-contained tokenizer + recursive-descent parser for the
openCypher subset defined in :mod:`needquery.cypher.ast`.

It is intentionally hand-written (no parser-generator dependency) so the whole
query surface is one readable file and the error messages point at a column. The
grammar it accepts:

    query      := 'MATCH' path (',' path)*
                  ('WHERE' expr)?
                  'RETURN' ('DISTINCT')? item (',' item)*
                  ('ORDER' 'BY' orderitem (',' orderitem)*)?
                  ('LIMIT' int)?
    path       := node (rel node)*
    node       := '(' var? (':' Label)? ('{' prop (',' prop)* '}')? ')'
    rel        := ('<-' | '-') '[' var? (':' type ('|' type)*)? star? ']' ('->' | '-')
                | ('<--' | '--' | '-->')            # anonymous relationship
    star       := '*' int? ('..' int?)?
    expr       := or
    or         := and ('OR' and)*
    and        := not ('AND' not)*
    not        := 'NOT' not | predicate
    predicate  := '(' path ')'                       # pattern predicate
                | comparison
    comparison := add (('=' | '<>' | ...) add | 'IS' 'NOT'? 'NULL')?
    add        := primary
    primary    := literal | var '.' key | var | '(' expr ')'

This is a strict subset — the parser raises :class:`CypherSyntaxError` on
anything outside it rather than silently accepting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import ast


class CypherSyntaxError(ValueError):
    pass


# -- tokenizer ------------------------------------------------------------

_KEYWORDS = {
    "MATCH", "WHERE", "RETURN", "DISTINCT", "ORDER", "BY", "LIMIT",
    "AND", "OR", "NOT", "IN", "IS", "NULL", "CONTAINS", "STARTS", "ENDS", "WITH",
    "TRUE", "FALSE", "ASC", "DESC",
}

# Order matters: longer operators before their prefixes.
_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<FLOAT>-?\d+\.\d+)
    | (?P<INT>-?\d+)
    | (?P<STRING>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    | (?P<ARROW_L>\<-)
    | (?P<ARROW_R>-\>)
    | (?P<DDOT>\.\.)
    | (?P<NE>\<\>)
    | (?P<LE>\<=)
    | (?P<GE>\>=)
    | (?P<DASH>-)
    | (?P<LT>\<)
    | (?P<GT>\>)
    | (?P<EQ>=)
    | (?P<STAR>\*)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACK>\[)
    | (?P<RBRACK>\])
    | (?P<LBRACE>\{)
    | (?P<RBRACE>\})
    | (?P<COMMA>,)
    | (?P<COLON>:)
    | (?P<DOT>\.)
    | (?P<PIPE>\|)
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


@dataclass(slots=True)
class Token:
    kind: str
    value: str
    col: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise CypherSyntaxError(f"unexpected character {text[pos]!r} at column {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        value = m.group()
        if kind == "NAME" and value.upper() in _KEYWORDS:
            kind = value.upper()
        tokens.append(Token(kind, value, m.start()))
    tokens.append(Token("EOF", "", n))
    return tokens


# -- parser ---------------------------------------------------------------

_COMPARATORS = {"EQ": "=", "NE": "<>", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._t = tokens
        self._i = 0

    # token helpers
    @property
    def _cur(self) -> Token:
        return self._t[self._i]

    def _at(self, *kinds: str) -> bool:
        return self._cur.kind in kinds

    def _eat(self, kind: str) -> Token:
        tok = self._cur
        if tok.kind != kind:
            raise CypherSyntaxError(
                f"expected {kind} but found {tok.kind} {tok.value!r} at column {tok.col}"
            )
        self._i += 1
        return tok

    def _accept(self, kind: str) -> Token | None:
        if self._cur.kind == kind:
            tok = self._cur
            self._i += 1
            return tok
        return None

    # entry point
    def parse(self) -> ast.Query:
        self._eat("MATCH")
        match = [self._path()]
        while self._accept("COMMA"):
            match.append(self._path())
        where = None
        if self._accept("WHERE"):
            where = self._expr()
        self._eat("RETURN")
        distinct = self._accept("DISTINCT") is not None
        returns = [self._return_item()]
        while self._accept("COMMA"):
            returns.append(self._return_item())
        order_by: list[ast.OrderItem] = []
        if self._accept("ORDER"):
            self._eat("BY")
            order_by.append(self._order_item())
            while self._accept("COMMA"):
                order_by.append(self._order_item())
        limit = None
        if self._accept("LIMIT"):
            limit = int(self._eat("INT").value)
        if not self._at("EOF"):
            raise CypherSyntaxError(
                f"unexpected trailing input {self._cur.value!r} at column {self._cur.col}"
            )
        return ast.Query(match, where, returns, distinct, order_by, limit)

    # -- patterns --
    def _path(self) -> ast.PathPattern:
        nodes = [self._node()]
        rels: list[ast.RelPattern] = []
        while self._at("DASH", "ARROW_L"):
            rels.append(self._rel())
            nodes.append(self._node())
        return ast.PathPattern(nodes, rels)

    def _node(self) -> ast.NodePattern:
        self._eat("LPAREN")
        var = None
        label = None
        if self._at("NAME"):
            var = self._eat("NAME").value
        if self._accept("COLON"):
            label = self._eat("NAME").value
        props: dict[str, ast.Expr] = {}
        if self._accept("LBRACE"):
            props = self._prop_map()
        self._eat("RPAREN")
        return ast.NodePattern(var, label, props)

    def _prop_map(self) -> dict[str, ast.Expr]:
        props: dict[str, ast.Expr] = {}
        while not self._at("RBRACE"):
            key = self._eat("NAME").value
            self._eat("COLON")
            props[key] = self._primary()
            if not self._accept("COMMA"):
                break
        self._eat("RBRACE")
        return props

    def _rel(self) -> ast.RelPattern:
        left_arrow = self._accept("ARROW_L") is not None
        if not left_arrow:
            self._eat("DASH")
        var = None
        types: tuple[str, ...] = ()
        min_hops, max_hops = 1, 1
        if self._accept("LBRACK"):
            if self._at("NAME"):
                var = self._eat("NAME").value
            if self._accept("COLON"):
                names = [self._eat("NAME").value]
                while self._accept("PIPE"):
                    names.append(self._eat("NAME").value)
                types = tuple(names)
            if self._accept("STAR"):
                min_hops, max_hops = self._star()
            self._eat("RBRACK")
        # closing side of the relationship
        right_arrow = self._accept("ARROW_R") is not None
        if not right_arrow:
            self._eat("DASH")
        if left_arrow and right_arrow:
            raise CypherSyntaxError("relationship cannot point both directions")
        direction = "in" if left_arrow else "out" if right_arrow else "both"
        return ast.RelPattern(var, types, direction, min_hops, max_hops)

    def _star(self) -> tuple[int, int | None]:
        # already consumed '*'
        lo: int = 1
        hi: int | None = None
        if self._at("INT"):
            lo = int(self._eat("INT").value)
            hi = lo
        if self._accept("DDOT"):
            if self._at("INT"):
                hi = int(self._eat("INT").value)
            else:
                hi = None  # unbounded upper
        elif hi is None:
            # bare '*' == 1..unbounded
            lo, hi = 1, None
        return lo, hi

    # -- expressions --
    def _expr(self) -> ast.Expr:
        return self._or()

    def _or(self) -> ast.Expr:
        operands = [self._and()]
        while self._accept("OR"):
            operands.append(self._and())
        return operands[0] if len(operands) == 1 else ast.BoolOp("OR", operands)

    def _and(self) -> ast.Expr:
        operands = [self._not()]
        while self._accept("AND"):
            operands.append(self._not())
        return operands[0] if len(operands) == 1 else ast.BoolOp("AND", operands)

    def _not(self) -> ast.Expr:
        if self._accept("NOT"):
            return ast.Not(self._not())
        return self._predicate()

    def _predicate(self) -> ast.Expr:
        # A parenthesised group is ambiguous: it is a pattern predicate iff it
        # begins a node pattern "( ... )" that is followed by a relationship, or
        # is a lone "(:Label)" / "()". We look ahead: '(' then (NAME? COLON? )
        # then ')' optionally followed by a relationship arrow.
        if self._at("LPAREN") and self._looks_like_pattern():
            return ast.PatternPredicate(self._path())
        return self._comparison()

    def _looks_like_pattern(self) -> bool:
        # Save position, try to see if "(...)" is a node pattern.
        j = self._i
        assert self._t[j].kind == "LPAREN"
        j += 1
        # node interior: NAME? (COLON NAME)? ...
        if self._t[j].kind == "NAME":
            j += 1
        if self._t[j].kind == "COLON":
            # (:Label) is unambiguously a node pattern
            return True
        if self._t[j].kind == "RPAREN":
            # "()" — anonymous node; pattern only if followed by a relationship
            k = j + 1
            return self._t[k].kind in ("DASH", "ARROW_L")
        return False

    def _comparison(self) -> ast.Expr:
        left = self._primary()
        # IS [NOT] NULL
        if self._accept("IS"):
            negated = self._accept("NOT") is not None
            self._eat("NULL")
            return ast.IsNull(left, negated)
        kind = self._cur.kind
        if kind in _COMPARATORS:
            self._i += 1
            return ast.Comparison(_COMPARATORS[kind], left, self._primary())
        if self._accept("IN"):
            return ast.Comparison("IN", left, self._primary())
        if self._accept("CONTAINS"):
            return ast.Comparison("CONTAINS", left, self._primary())
        if self._accept("STARTS"):
            self._eat("WITH")
            return ast.Comparison("STARTS_WITH", left, self._primary())
        if self._accept("ENDS"):
            self._eat("WITH")
            return ast.Comparison("ENDS_WITH", left, self._primary())
        return left

    def _primary(self) -> ast.Expr:
        tok = self._cur
        if tok.kind == "LPAREN":
            self._i += 1
            inner = self._expr()
            self._eat("RPAREN")
            return inner
        if tok.kind == "STRING":
            self._i += 1
            return ast.Literal(_unquote(tok.value))
        if tok.kind == "INT":
            self._i += 1
            return ast.Literal(int(tok.value))
        if tok.kind == "FLOAT":
            self._i += 1
            return ast.Literal(float(tok.value))
        if tok.kind in ("TRUE", "FALSE"):
            self._i += 1
            return ast.Literal(tok.kind == "TRUE")
        if tok.kind == "NULL":
            self._i += 1
            return ast.Literal(None)
        if tok.kind == "LBRACK":
            return self._list_literal()
        if tok.kind == "NAME":
            self._i += 1
            if self._accept("DOT"):
                key = self._eat("NAME").value
                return ast.Property(tok.value, key)
            return ast.Variable(tok.value)
        raise CypherSyntaxError(
            f"unexpected {tok.kind} {tok.value!r} at column {tok.col}"
        )

    def _list_literal(self) -> ast.Literal:
        self._eat("LBRACK")
        items: list = []
        while not self._at("RBRACK"):
            item = self._primary()
            if not isinstance(item, ast.Literal):
                raise CypherSyntaxError("only literal list elements are supported")
            items.append(item.value)
            if not self._accept("COMMA"):
                break
        self._eat("RBRACK")
        return ast.Literal(items)

    def _return_item(self) -> ast.ReturnItem:
        expr = self._primary()
        alias = self._default_alias(expr)
        if self._cur.kind == "NAME" and self._cur.value.upper() == "AS":
            self._i += 1
            alias = self._eat("NAME").value
        return ast.ReturnItem(expr, alias)

    def _order_item(self) -> ast.OrderItem:
        expr = self._primary()
        descending = False
        if self._accept("ASC"):
            descending = False
        elif self._accept("DESC"):
            descending = True
        return ast.OrderItem(expr, descending)

    @staticmethod
    def _default_alias(expr: ast.Expr) -> str:
        if isinstance(expr, ast.Property):
            return f"{expr.var}.{expr.key}"
        if isinstance(expr, ast.Variable):
            return expr.name
        return "value"


def _unquote(raw: str) -> str:
    body = raw[1:-1]
    return body.encode().decode("unicode_escape")


def parse(query: str) -> ast.Query:
    """Parse a Cypher string into a :class:`~needquery.cypher.ast.Query`."""
    return Parser(tokenize(query)).parse()
