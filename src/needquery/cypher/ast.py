"""AST for the openCypher subset we support.

The subset is deliberately the slice that real ``needtable`` / ``needflow``
selections use: labelled node patterns, directed/undirected relationship
patterns with optional variable length, a ``WHERE`` with the usual comparison
and boolean operators plus *pattern predicates* (``NOT (a)-->(b)``), and a
``RETURN`` with optional ``ORDER BY`` / ``LIMIT``. It is not the whole language;
that is the honest boundary and the reference evaluator enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# -- patterns -------------------------------------------------------------

@dataclass(slots=True)
class NodePattern:
    var: str | None
    label: str | None
    props: dict[str, "Expr"] = field(default_factory=dict)


@dataclass(slots=True)
class RelPattern:
    var: str | None
    types: tuple[str, ...]  # empty tuple = any relationship type
    direction: str  # "out" | "in" | "both"
    min_hops: int = 1
    max_hops: int | None = 1  # None = unbounded


@dataclass(slots=True)
class PathPattern:
    """node (rel node)* — a linear path. Enough for traceability queries."""

    nodes: list[NodePattern]
    rels: list[RelPattern]  # len == len(nodes) - 1


# -- expressions ----------------------------------------------------------

@dataclass(slots=True)
class Literal:
    value: Any


@dataclass(slots=True)
class Property:
    var: str
    key: str


@dataclass(slots=True)
class Variable:
    name: str


@dataclass(slots=True)
class Comparison:
    op: str  # = <> < <= > >= IN CONTAINS STARTS_WITH ENDS_WITH
    left: "Expr"
    right: "Expr"


@dataclass(slots=True)
class IsNull:
    operand: "Expr"
    negated: bool


@dataclass(slots=True)
class BoolOp:
    op: str  # AND | OR
    operands: list["Expr"]


@dataclass(slots=True)
class Not:
    operand: "Expr"


@dataclass(slots=True)
class PatternPredicate:
    """Existence of a path, used as a boolean, e.g. ``NOT ( ()-->(n) )``."""

    path: PathPattern


Expr = (
    Literal
    | Property
    | Variable
    | Comparison
    | IsNull
    | BoolOp
    | Not
    | PatternPredicate
)


# -- query ----------------------------------------------------------------

@dataclass(slots=True)
class ReturnItem:
    expr: Expr
    alias: str


@dataclass(slots=True)
class OrderItem:
    expr: Expr
    descending: bool


@dataclass(slots=True)
class Query:
    match: list[PathPattern]
    where: Expr | None
    returns: list[ReturnItem]
    distinct: bool = False
    order_by: list[OrderItem] = field(default_factory=list)
    limit: int | None = None
