"""Executor for the openCypher subset.

The executor is where the "an engine can plan an optimal execution path" claim
is made concrete. Two decisions matter for performance:

1. **Selective anchor.** For each path pattern, matching starts at the node
   pattern with the smallest candidate set (fewest nodes of that label), not at
   the left-most one. Expanding outward from 2 untraced ``swreq`` nodes is
   cheaper than starting from 69 ``test`` nodes and filtering.
2. **Index-only expansion.** A hop follows the precomputed typed adjacency list;
   it never scans the node set. Variable-length hops are a BFS with a visited
   set, so cycles in the graph terminate instead of looping forever — the exact
   failure mode a hand-written Python ``filter_code`` traversal hits.

The result is a list of rows (dicts keyed by RETURN alias). Node-valued RETURN
items yield the need id string, matching what ``ubc query cypher`` returns for a
bare node, so the two backends are directly comparable.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..graph import Node, PropertyGraph
from . import ast


class CypherEvalError(ValueError):
    pass


# A binding maps a pattern variable (or a positional key for an anonymous node)
# to a concrete node id.
Binding = dict[str, str]


def _key(np: "ast.NodePattern", pos: int) -> str:
    """The binding key for a node pattern: its variable, or a positional slot."""
    return np.var if np.var else f"__n{pos}"


class Executor:
    def __init__(self, graph: PropertyGraph) -> None:
        self.g = graph

    def run(self, query: ast.Query) -> list[dict[str, Any]]:
        bindings = self._match_all(query.match)
        if query.where is not None:
            bindings = [b for b in bindings if self._truthy(query.where, b)]
        rows = [self._project(query.returns, b) for b in bindings]
        if query.distinct:
            rows = _dedupe(rows)
        if query.order_by:
            for oi in reversed(query.order_by):
                rows.sort(
                    key=lambda r, oi=oi: _sort_key(self._eval_order(oi.expr, r)),
                    reverse=oi.descending,
                )
        if query.limit is not None:
            rows = rows[: query.limit]
        return rows

    # -- matching ----------------------------------------------------------

    def _match_all(self, paths: list[ast.PathPattern]) -> list[Binding]:
        bindings: list[Binding] = [{}]
        for path in paths:
            nxt: list[Binding] = []
            for b in bindings:
                nxt.extend(self._match_path(path, b))
            bindings = nxt
        return bindings

    def _match_path(self, path: ast.PathPattern, base: Binding) -> Iterator[Binding]:
        anchor = self._anchor_index(path, base)
        for start_id in self._candidates(path.nodes[anchor], base):
            b = dict(base)
            if not self._bind_node(path.nodes[anchor], anchor, start_id, b):
                continue
            yield from self._expand(path, anchor, b)

    def _anchor_index(self, path: ast.PathPattern, base: Binding) -> int:
        """Pick the cheapest node pattern to start from."""
        best_i, best_cost = 0, None
        for i, np in enumerate(path.nodes):
            if np.var and np.var in base:
                return i  # an already-bound variable is a single node: start there
            cost = self.g.label_count(np.label) if np.label else len(self.g)
            if best_cost is None or cost < best_cost:
                best_i, best_cost = i, cost
        return best_i

    def _candidates(self, np: ast.NodePattern, base: Binding) -> Iterator[str]:
        if np.var and np.var in base:
            yield base[np.var]
            return
        yield from self.g.ids_with_label(np.label)

    def _expand(self, path: ast.PathPattern, anchor: int, b: Binding) -> Iterator[Binding]:
        """Grow the match rightward then leftward from a bound anchor node.

        Every node position is keyed (named var or a positional ``__nK``), so a
        later hop always reads a concrete id — anonymous nodes thread through a
        multi-hop path just like named ones."""

        def go_right(idx: int, cur: Binding) -> Iterator[Binding]:
            if idx >= len(path.rels):
                yield from go_left(cur)
                return
            src = cur[_key(path.nodes[idx], idx)]
            for dst in self._traverse(src, path.rels[idx]):
                nb = dict(cur)
                if self._bind_node(path.nodes[idx + 1], idx + 1, dst, nb):
                    yield from go_right(idx + 1, nb)

        def go_left(cur: Binding) -> Iterator[Binding]:
            def step(i: int, c: Binding) -> Iterator[Binding]:
                if i < 0:
                    yield c
                    return
                # edge nodes[i] -rel-> nodes[i+1]; nodes[i+1] known, seek nodes[i]
                dst = c[_key(path.nodes[i + 1], i + 1)]
                for src in self._traverse_reverse(dst, path.rels[i]):
                    nc = dict(c)
                    if self._bind_node(path.nodes[i], i, src, nc):
                        yield from step(i - 1, nc)
            yield from step(anchor - 1, cur)

        yield from go_right(anchor, b)

    def _bind_node(
        self, np: ast.NodePattern, pos: int, nid: str, b: Binding
    ) -> bool:
        node = self.g.node(nid)
        if node is None:
            return False
        if np.label is not None and node.label != np.label:
            return False
        for key, expr in np.props.items():
            if node[key] != self._literal(expr):
                return False
        k = _key(np, pos)
        if k in b and b[k] != nid:  # join consistency (repeated var must agree)
            return False
        b[k] = nid
        return True

    def _traverse(self, src: str, rel: ast.RelPattern) -> Iterator[str]:
        rels = rel.types or None
        if rel.min_hops == 1 and rel.max_hops == 1:
            yield from self.g.neighbours(src, rels, rel.direction)
            return
        yield from self._varlen(src, rel, rels)

    def _traverse_reverse(self, dst: str, rel: ast.RelPattern) -> Iterator[str]:
        # reverse of an "out" edge is an "in" edge, and vice versa
        inv = {"out": "in", "in": "out", "both": "both"}[rel.direction]
        rr = ast.RelPattern(rel.var, rel.types, inv, rel.min_hops, rel.max_hops)
        yield from self._traverse(dst, rr)

    def _varlen(
        self, src: str, rel: ast.RelPattern, rels: tuple[str, ...] | None
    ) -> Iterator[str]:
        """BFS for variable-length paths.

        Two sets keep this both correct and terminating:

        * ``expanded`` — nodes whose neighbours we have already followed. A node
          is expanded at most once, so a cyclic graph terminates (this is the
          guard a hand-written filter_code traversal forgets).
        * ``emitted`` — nodes already returned, so each result appears once.

        The start node is *not* pre-excluded from emission: if it is reachable
        from itself via an edge (a self-loop or a cycle), it is a legitimate
        match at length >= 1, exactly as Cypher's relationship-unique semantics
        intend."""
        lo = rel.min_hops
        hi = rel.max_hops  # None = unbounded
        frontier = {src}
        expanded: set[str] = set()
        emitted: set[str] = set()
        depth = 0
        while frontier and (hi is None or depth < hi):
            depth += 1
            nxt: set[str] = set()
            for node in frontier:
                if node in expanded:
                    continue
                expanded.add(node)
                for nb in self.g.neighbours(node, rels, rel.direction):
                    if depth >= lo and nb not in emitted:
                        emitted.add(nb)
                        yield nb
                    if nb not in expanded:
                        nxt.add(nb)
            frontier = nxt

    # -- WHERE evaluation --------------------------------------------------

    def _truthy(self, expr: ast.Expr, b: Binding) -> bool:
        val = self._eval(expr, b)
        return bool(val)

    def _eval(self, expr: ast.Expr, b: Binding) -> Any:
        match expr:
            case ast.Literal(value):
                return value
            case ast.Variable(name):
                return b.get(name)
            case ast.Property(var, key):
                nid = b.get(var)
                node = self.g.node(nid) if nid else None
                if node is None:
                    return None
                # id and type are built-in properties (matching ubc), always
                # available even when not materialised in the attribute map.
                if key == "id":
                    return node.id
                if key == "type":
                    return node.label
                return node[key]
            case ast.Not(operand):
                return not self._truthy(operand, b)
            case ast.BoolOp(op, operands):
                if op == "AND":
                    return all(self._truthy(o, b) for o in operands)
                return any(self._truthy(o, b) for o in operands)
            case ast.IsNull(operand, negated):
                is_null = self._eval(operand, b) is None
                return (not is_null) if negated else is_null
            case ast.Comparison(op, left, right):
                return self._compare(op, self._eval(left, b), self._eval(right, b))
            case ast.PatternPredicate(path):
                # existence: does at least one match of `path` extend `b`?
                for _ in self._match_path(path, b):
                    return True
                return False
        raise CypherEvalError(f"cannot evaluate {expr!r}")

    @staticmethod
    def _compare(op: str, a: Any, b: Any) -> bool:
        if op == "=":
            return a == b
        if op == "<>":
            return a != b
        if a is None or b is None:
            return False  # SQL/Cypher-style: comparisons with null are not true
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "IN":
            return a in b
        if op == "CONTAINS":
            return str(b) in str(a)
        if op == "STARTS_WITH":
            return str(a).startswith(str(b))
        if op == "ENDS_WITH":
            return str(a).endswith(str(b))
        raise CypherEvalError(f"unknown operator {op}")

    # -- projection --------------------------------------------------------

    def _project(self, items: list[ast.ReturnItem], b: Binding) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for item in items:
            row[item.alias] = self._eval_return(item.expr, b)
        return row

    def _eval_return(self, expr: ast.Expr, b: Binding) -> Any:
        # A bare node variable projects to the need id (comparable with ubc).
        if isinstance(expr, ast.Variable):
            return b.get(expr.name)
        return self._eval(expr, b)

    def _eval_order(self, expr: ast.Expr, row: dict[str, Any]) -> Any:
        if isinstance(expr, ast.Property):
            return row.get(f"{expr.var}.{expr.key}")
        if isinstance(expr, ast.Variable):
            return row.get(expr.name)
        return None

    @staticmethod
    def _literal(expr: ast.Expr) -> Any:
        if isinstance(expr, ast.Literal):
            return expr.value
        raise CypherEvalError("node property patterns must be literals")


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        key = tuple(sorted((k, _hashable(v)) for k, v in r.items()))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _hashable(v: Any) -> Any:
    return tuple(v) if isinstance(v, list) else v


def _sort_key(v: Any):
    # None sorts last regardless of direction reversal quirks
    return (v is None, v)
