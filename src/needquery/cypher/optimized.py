# cython: language_level=3
"""An optimized executor for the same openCypher subset — how far pure Python
can actually be driven.

The naive reference executor (`executor.py`) interprets the query AST per row.
That is the honest baseline, and it *loses* to an expert who hand-builds an
index (bench workload B). This module closes that gap with three classic query-
planner techniques, then the whole file is optionally compiled with Cython
(`scripts/build_cython.sh`) for the constant-factor tail:

1. **Predicate pushdown.** ``WHERE h.id = 'SG_0'`` restricts the candidate set
   for ``h`` to one node *before* matching, instead of matching every
   ``safety_goal`` and filtering after. Equality conjuncts on any property are
   pushed; ``id`` pushdown collapses a label scan to a single node.
2. **Subquery decorrelation.** A pattern predicate such as
   ``NOT ( (r)<-[:links]-(:test) )`` is not re-matched per candidate row.
   The set of all ids for which the pattern holds is computed **once** with a
   single global match (a semi-join build side), and the per-row test becomes a
   set-membership check — exactly the reverse index the expert builds by hand.
3. **Expression compilation.** The WHERE tree is compiled once into nested
   Python closures; per-row evaluation is then direct calls with no AST
   dispatch, no isinstance ladder, no match statement.

The module is deliberately self-contained (no inheritance from the reference
executor, no ``match`` statements) so Cython can compile it as-is in
pure-Python mode. Same subset, same semantics: `tests/test_engine.py` runs the
full suite against both executors, and the parity harness accepts either.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from ..graph import PropertyGraph
from . import ast

Binding = dict


class CypherEvalError(ValueError):
    pass


def _key(np, pos):
    return np.var if np.var else f"__n{pos}"


class OptimizedExecutor:
    """Planned execution over the same AST the reference executor runs."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.g = graph
        # var -> [(prop_key, literal)] equality conjuncts pushed into matching
        self._pushed: dict[str, list[tuple[str, Any]]] = {}

    # -- entry point -------------------------------------------------------

    def run(self, query: ast.Query) -> list[dict[str, Any]]:
        outer_vars = self._outer_vars(query.match)
        self._pushed = self._extract_pushdowns(query.where)
        where_fn = self._compile(query.where, outer_vars) if query.where is not None else None
        try:
            bindings = self._filter_single_node(query.match, where_fn)
            if bindings is None:
                bindings = self._match_all(query.match)
                if where_fn is not None:
                    bindings = [b for b in bindings if where_fn(b)]
        finally:
            self._pushed = {}
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

    def _filter_single_node(self, paths, where_fn) -> list[Binding] | None:
        """The hot shape — one node pattern, no relationships — evaluated with
        a single reused binding dict. The generic path allocates one dict per
        candidate row; here only rows that survive WHERE get one. Returns None
        when the query is not this shape."""
        if len(paths) != 1 or paths[0].rels or paths[0].nodes[0].props:
            return None
        np = paths[0].nodes[0]
        k = _key(np, 0)
        label = np.label
        g = self.g
        shared: Binding = {}
        out: list[Binding] = []
        for nid in self._candidates(np, {}):
            if label is not None:
                node = g.node(nid)
                if node is None or node.label != label:
                    continue
            shared[k] = nid
            if where_fn is None or where_fn(shared):
                out.append({k: nid})
        return out

    # -- planning ----------------------------------------------------------

    @staticmethod
    def _outer_vars(paths) -> set:
        out: set = set()
        for path in paths:
            for np in path.nodes:
                if np.var:
                    out.add(np.var)
        return out

    def _extract_pushdowns(self, where) -> dict[str, list[tuple[str, Any]]]:
        """Collect ``var.key = literal`` conjuncts from the top-level AND."""
        pushed: dict[str, list[tuple[str, Any]]] = {}

        def visit(expr) -> None:
            if isinstance(expr, ast.BoolOp) and expr.op == "AND":
                for o in expr.operands:
                    visit(o)
                return
            if isinstance(expr, ast.Comparison) and expr.op == "=":
                left, right = expr.left, expr.right
                if isinstance(right, ast.Property) and isinstance(left, ast.Literal):
                    left, right = right, left
                if isinstance(left, ast.Property) and isinstance(right, ast.Literal):
                    pushed.setdefault(left.var, []).append((left.key, right.value))

        if where is not None:
            visit(where)
        return pushed

    # -- matching (pushdown-aware) ----------------------------------------

    def _match_all(self, paths) -> list[Binding]:
        bindings: list[Binding] = [{}]
        for path in paths:
            nxt: list[Binding] = []
            for b in bindings:
                nxt.extend(self._match_path(path, b))
            bindings = nxt
        return bindings

    def _match_path(self, path, base: Binding) -> Iterator[Binding]:
        # Fast path: a single node pattern with no relationships and no prior
        # bindings needs no dict copying and no generic bind machinery — one
        # small dict per candidate. This is the hot shape of every flat filter
        # and every decorrelated-predicate outer loop.
        if not path.rels and not base and not path.nodes[0].props:
            np = path.nodes[0]
            k = _key(np, 0)
            label = np.label
            g = self.g
            for nid in self._candidates(np, base):
                if label is not None:
                    node = g.node(nid)
                    if node is None or node.label != label:
                        continue
                yield {k: nid}
            return
        anchor = self._anchor_index(path, base)
        for start_id in self._candidates(path.nodes[anchor], base):
            b = dict(base)
            if not self._bind_node(path.nodes[anchor], anchor, start_id, b):
                continue
            yield from self._expand(path, anchor, b)

    def _anchor_index(self, path, base: Binding) -> int:
        best_i, best_cost = 0, None
        for i, np in enumerate(path.nodes):
            if np.var and np.var in base:
                return i
            if np.var and any(k == "id" for k, _ in self._pushed.get(np.var, ())):
                return i  # a pushed id-equality pins this pattern to one node
            cost = self.g.label_count(np.label) if np.label else len(self.g)
            if best_cost is None or cost < best_cost:
                best_i, best_cost = i, cost
        return best_i

    def _candidates(self, np, base: Binding) -> Iterator[str]:
        if np.var and np.var in base:
            yield base[np.var]
            return
        eqs = self._pushed.get(np.var, ()) if np.var else ()
        for key, val in eqs:
            if key == "id":
                if self.g.node(val) is not None:
                    yield val
                return
        if eqs:
            g = self.g
            for nid in g.ids_with_label(np.label):
                node = g.node(nid)
                ok = True
                for key, val in eqs:
                    v = node.label if key == "type" else node[key]
                    if v != val:
                        ok = False
                        break
                if ok:
                    yield nid
            return
        yield from self.g.ids_with_label(np.label)

    def _expand(self, path, anchor: int, b: Binding) -> Iterator[Binding]:
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
                dst = c[_key(path.nodes[i + 1], i + 1)]
                for src in self._traverse_reverse(dst, path.rels[i]):
                    nc = dict(c)
                    if self._bind_node(path.nodes[i], i, src, nc):
                        yield from step(i - 1, nc)

            yield from step(anchor - 1, cur)

        yield from go_right(anchor, b)

    def _bind_node(self, np, pos: int, nid: str, b: Binding) -> bool:
        node = self.g.node(nid)
        if node is None:
            return False
        if np.label is not None and node.label != np.label:
            return False
        for key, expr in np.props.items():
            if node[key] != self._literal(expr):
                return False
        k = _key(np, pos)
        if k in b and b[k] != nid:
            return False
        b[k] = nid
        return True

    def _traverse(self, src: str, rel) -> Iterator[str]:
        rels = rel.types or None
        if rel.min_hops == 1 and rel.max_hops == 1:
            yield from self.g.neighbours(src, rels, rel.direction)
            return
        yield from self._varlen(src, rel, rels)

    def _traverse_reverse(self, dst: str, rel) -> Iterator[str]:
        inv = {"out": "in", "in": "out", "both": "both"}[rel.direction]
        rr = ast.RelPattern(rel.var, rel.types, inv, rel.min_hops, rel.max_hops)
        yield from self._traverse(dst, rr)

    def _varlen(self, src: str, rel, rels) -> Iterator[str]:
        lo = rel.min_hops
        hi = rel.max_hops
        frontier = {src}
        expanded: set = set()
        emitted: set = set()
        depth = 0
        while frontier and (hi is None or depth < hi):
            depth += 1
            nxt: set = set()
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

    # -- WHERE compilation -------------------------------------------------

    def _compile(self, expr, outer_vars: set) -> Callable[[Binding], Any]:
        """Compile the WHERE tree once into nested closures."""
        if isinstance(expr, ast.Literal):
            v = expr.value
            return lambda b: v
        if isinstance(expr, ast.Variable):
            name = expr.name
            return lambda b: b.get(name)
        if isinstance(expr, ast.Property):
            var, key, g = expr.var, expr.key, self.g
            if key == "id":
                return lambda b: b.get(var)
            if key == "type":
                return lambda b: (n := g.node(b[var])) is not None and n.label if var in b else None
            def prop(b, var=var, key=key, g=g):
                nid = b.get(var)
                if nid is None:
                    return None
                node = g.node(nid)
                return None if node is None else node[key]
            return prop
        if isinstance(expr, ast.Not):
            f = self._compile(expr.operand, outer_vars)
            return lambda b: not f(b)
        if isinstance(expr, ast.BoolOp):
            fns = [self._compile(o, outer_vars) for o in expr.operands]
            if expr.op == "AND":
                return lambda b: all(f(b) for f in fns)
            return lambda b: any(f(b) for f in fns)
        if isinstance(expr, ast.IsNull):
            f = self._compile(expr.operand, outer_vars)
            if expr.negated:
                return lambda b: f(b) is not None
            return lambda b: f(b) is None
        if isinstance(expr, ast.Comparison):
            lf = self._compile(expr.left, outer_vars)
            rf = self._compile(expr.right, outer_vars)
            return _compiled_compare(expr.op, lf, rf)
        if isinstance(expr, ast.PatternPredicate):
            return self._compile_pattern(expr.path, outer_vars)
        raise CypherEvalError(f"cannot compile {expr!r}")

    def _compile_pattern(self, path, outer_vars: set) -> Callable[[Binding], bool]:
        """Decorrelate a pattern predicate when it references exactly one
        variable bound by the outer MATCH: build the set of ids for which the
        pattern holds with ONE global match, then test membership per row."""
        pred_vars = [np.var for np in path.nodes if np.var and np.var in outer_vars]
        if len(pred_vars) == 1:
            var = pred_vars[0]
            pos = next(i for i, np in enumerate(path.nodes) if np.var == var)
            holds = self._pattern_set(path, pos)
            return lambda b: b.get(var) in holds
        if not pred_vars:  # uncorrelated: a constant — evaluate once
            const = any(True for _ in self._match_path(path, {}))
            return lambda b: const

        # two or more correlated vars: fall back to per-row existence
        def exists(b, path=path):
            for _ in self._match_path(path, b):
                return True
            return False

        return exists

    def _pattern_set(self, path, pos: int):
        """All ids that can appear at position ``pos`` of ``path`` — the
        semi-join build side. Single-hop patterns get a direct adjacency sweep
        (exactly the reverse index an expert hand-writes); anything longer runs
        through the generic matcher once."""
        g = self.g
        np_c = path.nodes[pos]
        if (
            len(path.nodes) == 2
            and len(path.rels) == 1
            and path.rels[0].min_hops == 1
            and path.rels[0].max_hops == 1
            and not path.nodes[0].props
            and not path.nodes[1].props
        ):
            rel = path.rels[0]
            other = path.nodes[1 - pos]
            rels = rel.types or None
            # rel.direction is stated left-to-right; walking from `other`
            # toward the correlated position flips it when other is on the left.
            if pos == 1:
                direction = rel.direction
            else:
                direction = {"out": "in", "in": "out", "both": "both"}[rel.direction]
            if other.label is None:
                # The other endpoint is unconstrained, so "reachable from any
                # node" is just "has an edge in the inverse direction" — and
                # the graph already indexes exactly that. No sweep at all.
                inv = {"out": "in", "in": "out", "both": "both"}[direction]
                holds = set(g.ids_with_edge(rels, inv))
            else:
                holds = set()
                for oid in g.ids_with_label(other.label):
                    for nb in g.neighbours(oid, rels, direction):
                        holds.add(nb)
            if np_c.label is not None:
                holds = {i for i in holds if g.node(i).label == np_c.label}
            return frozenset(holds)
        k = _key(np_c, pos)
        return frozenset(b[k] for b in self._match_path(path, {}))

    # -- projection (same semantics as the reference executor) -------------

    def _project(self, items, b: Binding) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for item in items:
            row[item.alias] = self._eval_return(item.expr, b)
        return row

    def _eval_return(self, expr, b: Binding) -> Any:
        if isinstance(expr, ast.Variable):
            return b.get(expr.name)
        return self._compile(expr, set())(b)

    def _eval_order(self, expr, row: dict[str, Any]) -> Any:
        if isinstance(expr, ast.Property):
            return row.get(f"{expr.var}.{expr.key}")
        if isinstance(expr, ast.Variable):
            return row.get(expr.name)
        return None

    @staticmethod
    def _literal(expr) -> Any:
        if isinstance(expr, ast.Literal):
            return expr.value
        raise CypherEvalError("node property patterns must be literals")


def _compiled_compare(op: str, lf, rf) -> Callable[[Binding], bool]:
    if op == "=":
        return lambda b: lf(b) == rf(b)
    if op == "<>":
        return lambda b: lf(b) != rf(b)

    def guarded(fn):
        def cmp(b):
            a, c = lf(b), rf(b)
            if a is None or c is None:
                return False  # Cypher-style: comparisons with null are not true
            return fn(a, c)
        return cmp

    if op == "<":
        return guarded(lambda a, c: a < c)
    if op == "<=":
        return guarded(lambda a, c: a <= c)
    if op == ">":
        return guarded(lambda a, c: a > c)
    if op == ">=":
        return guarded(lambda a, c: a >= c)
    if op == "IN":
        return guarded(lambda a, c: a in c)
    if op == "CONTAINS":
        return guarded(lambda a, c: str(c) in str(a))
    if op == "STARTS_WITH":
        return guarded(lambda a, c: str(a).startswith(str(c)))
    if op == "ENDS_WITH":
        return guarded(lambda a, c: str(a).endswith(str(c)))
    raise CypherEvalError(f"unknown operator {op}")


def _dedupe(rows):
    seen: set = set()
    out = []
    for r in rows:
        key = tuple(sorted((k, _hashable(v)) for k, v in r.items()))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _hashable(v):
    return tuple(v) if isinstance(v, list) else v


def _sort_key(v):
    return (v is None, v)
