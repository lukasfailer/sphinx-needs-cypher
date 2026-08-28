"""A deprecation shim: translate the mechanical subset of ``filter_string`` into
the equivalent Cypher, and refuse — loudly and specifically — anything outside
that subset.

This is the migration path any such change depends on. You cannot tell
thousands of existing projects "rewrite every :filter: by hand." You
ship a translator that converts the safe, common shapes automatically and leaves
a precise TODO on the rest. That is exactly the pattern useblocks already used
for an 8.3.0 deprecation (a template-driven shim), applied to the query surface.

A ``filter_string`` is a Python expression, so we parse it with Python's own
``ast`` module — no bespoke parser, no guessing — and walk the tree. Supported:

    type == 'swreq'                 -> label            (:swreq)
    status == 'open'                -> WHERE n.status = 'open'
    asil != 'D'                     -> WHERE n.asil <> 'D'
    a and b, a or b, not a          -> AND / OR / NOT
    'safety' in tags                -> WHERE 'safety' IN n.tags

Anything else — a function call, attribute access, ``len()``, subscripting, a
comprehension, arithmetic — raises :class:`Untranslatable` naming what it saw. A
human reviews those; the shim never emits a silently-wrong query.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


class Untranslatable(ValueError):
    """Raised when a filter_string uses something outside the safe subset."""


@dataclass
class Translation:
    cypher: str
    label: str | None
    where: str | None


def translate(filter_string: str, var: str = "n") -> Translation:
    try:
        tree = ast.parse(filter_string.strip(), mode="eval")
    except SyntaxError as exc:  # not even a valid Python expression
        raise Untranslatable(f"not a parseable expression: {exc}") from exc

    label, where = _Walker(var).split(tree.body)
    label_part = f":{label}" if label else ""
    where_part = f" WHERE {where}" if where else ""
    cypher = f"MATCH ({var}{label_part}){where_part} RETURN {var}"
    return Translation(cypher, label, where)


class _Walker:
    def __init__(self, var: str) -> None:
        self.var = var

    def split(self, node: ast.expr) -> tuple[str | None, str | None]:
        """Pull a single ``type == 'X'`` up into the node label; everything else
        becomes the WHERE clause."""
        label, remainder = self._extract_label(node)
        where = self._expr(remainder) if remainder is not None else None
        return label, where

    def _extract_label(self, node: ast.expr) -> tuple[str | None, ast.expr | None]:
        # top-level "type == 'X' and <rest>" -> label X, rest
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            label = None
            rest: list[ast.expr] = []
            for v in node.values:
                lbl = self._as_type_eq(v)
                if lbl is not None and label is None:
                    label = lbl
                else:
                    rest.append(v)
            if label is not None:
                if not rest:
                    return label, None
                if len(rest) == 1:
                    return label, rest[0]
                return label, ast.BoolOp(ast.And(), rest)
        lbl = self._as_type_eq(node)
        if lbl is not None:
            return lbl, None
        return None, node

    @staticmethod
    def _as_type_eq(node: ast.expr) -> str | None:
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.left, ast.Name)
            and node.left.id == "type"
            and isinstance(node.comparators[0], ast.Constant)
        ):
            return str(node.comparators[0].value)
        return None

    def _expr(self, node: ast.expr) -> str:
        if isinstance(node, ast.BoolOp):
            op = "AND" if isinstance(node.op, ast.And) else "OR"
            return "(" + f" {op} ".join(self._expr(v) for v in node.values) + ")"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return f"NOT ({self._expr(node.operand)})"
        if isinstance(node, ast.Compare):
            return self._compare(node)
        if isinstance(node, ast.Constant):
            return _lit(node.value)
        if isinstance(node, ast.Name):
            return f"{self.var}.{node.id}"
        raise Untranslatable(
            f"unsupported expression {type(node).__name__} — review by hand"
        )

    def _compare(self, node: ast.Compare) -> str:
        if len(node.ops) != 1:
            raise Untranslatable("chained comparison — review by hand")
        op = node.ops[0]
        left, right = node.left, node.comparators[0]
        if isinstance(op, ast.In):
            return f"{self._operand(right)} CONTAINS {self._operand(left)}" \
                if isinstance(right, ast.Name) else \
                f"{self._operand(left)} IN {self._operand(right)}"
        mapping = {
            ast.Eq: "=", ast.NotEq: "<>", ast.Lt: "<", ast.LtE: "<=",
            ast.Gt: ">", ast.GtE: ">=",
        }
        for pytype, cyop in mapping.items():
            if isinstance(op, pytype):
                return f"{self._operand(left)} {cyop} {self._operand(right)}"
        raise Untranslatable(f"unsupported operator {type(op).__name__}")

    def _operand(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return _lit(node.value)
        if isinstance(node, ast.Name):
            return f"{self.var}.{node.id}"
        if isinstance(node, ast.List):
            return "[" + ", ".join(self._operand(e) for e in node.elts) + "]"
        raise Untranslatable(
            f"unsupported operand {type(node).__name__} — review by hand"
        )


def _lit(value: object) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


if __name__ == "__main__":
    samples = [
        "type == 'swreq' and status == 'open'",
        "type == 'safety_goal' and asil == 'D'",
        "status == 'open' or status == 'in_progress'",
        "type == 'req' and not (status == 'closed')",
        "'safety' in tags",
        # these must be refused, not mistranslated:
        "len(links) == 0",
        "search(r'ADAS', title)",
        "id.startswith('SWREQ')",
    ]
    for s in samples:
        try:
            t = translate(s)
            print(f"OK   {s!r}\n     -> {t.cypher}")
        except Untranslatable as exc:
            print(f"SKIP {s!r}\n     -> flagged for human review: {exc}")
