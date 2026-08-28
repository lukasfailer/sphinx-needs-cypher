"""The bitmap executor — a third fork of the planner.

The planned executor (`optimized.py`) already picks good algorithms; its
remaining cost is *per-row interpretation*: every candidate need pays dict
lookups and closure calls. This fork removes the per-row work entirely for the
hot query shape by changing the data representation, not the algorithm:

* **Interned integer ids.** Every need id is assigned a position ``0..N-1``
  once, at index build.
* **Bitmaps as Python big-ints.** For each queried attribute value, each label,
  and each edge-existence predicate, the index holds one integer with bit ``i``
  set when need ``i`` qualifies. A Python ``int`` *is* an arbitrary-length
  bitset, and ``&``, ``|``, ``~`` on it run in C — one machine instruction per
  64 needs. This is the pure-Python rendition of the Roaring-bitmap technique
  production engines use.
* **WHERE becomes bit-algebra.** ``type='swreq' AND NOT (()-->(r))`` lowers to
  ``label_bm & ~incoming_bm``: two big-int operations, regardless of N.

Scope is deliberate: single-node MATCH with equality / inequality / IN /
IS NULL / single-hop pattern predicates, combined with AND / OR / NOT. Every
other query shape falls back to the planned executor unchanged, so semantics
can never diverge — `tests/test_bitmap.py` holds this fork to bit-identical
results against the reference interpreter, and `last_path` exposes which route
ran so the tests can prove the bitmap path was actually taken.

Bitmap columns are built lazily, on the first query that needs them, and
memoized on the executor — the same warm-index treatment the benchmark gives
every engine lane.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..graph import PropertyGraph
from . import ast
from .optimized import OptimizedExecutor, _dedupe

_INV = {"out": "in", "in": "out", "both": "both"}
_HASHABLE = (str, int, float, bool, type(None))


class BitmapIndex:
    """Interned ids + lazily built big-int bitmaps over a PropertyGraph."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.g = graph
        self.ids: list[str] = [n.id for n in graph.nodes()]  # load order
        self.pos: dict[str, int] = {nid: i for i, nid in enumerate(self.ids)}
        n = len(self.ids)
        self.nbits = n
        self.universe = (1 << n) - 1
        self._labels: dict[str, int] | None = None
        self._attrs: dict[str, dict[Any, int]] = {}
        self._edges: dict[tuple, int] = {}
        self._semi: dict[tuple, int] = {}

    # -- builders (bytearray first, one C conversion at the end) -----------

    def _from_positions(self, positions) -> int:
        buf = bytearray((self.nbits + 7) // 8)
        for i in positions:
            buf[i >> 3] |= 1 << (i & 7)
        return int.from_bytes(buf, "little")

    def label_bm(self, label: str | None) -> int:
        if label is None:
            return self.universe
        if self._labels is None:
            bufs: dict[str, bytearray] = defaultdict(lambda: bytearray((self.nbits + 7) // 8))
            g = self.g
            for i, nid in enumerate(self.ids):
                buf = bufs[g.node(nid).label]
                buf[i >> 3] |= 1 << (i & 7)
            self._labels = {lab: int.from_bytes(b, "little") for lab, b in bufs.items()}
        return self._labels.get(label, 0)

    def attr_bm(self, key: str, value: Any) -> int:
        col = self._attrs.get(key)
        if col is None:
            bufs: dict[Any, bytearray] = defaultdict(lambda: bytearray((self.nbits + 7) // 8))
            g = self.g
            for i, nid in enumerate(self.ids):
                v = g.node(nid)[key]
                try:
                    buf = bufs[v]
                except TypeError:
                    continue  # unhashable (list/dict): can never equal a scalar literal
                buf[i >> 3] |= 1 << (i & 7)
            col = {v: int.from_bytes(b, "little") for v, b in bufs.items()}
            self._attrs[key] = col
        return col.get(value, 0)

    def id_bm(self, nid: Any) -> int:
        p = self.pos.get(nid) if isinstance(nid, str) else None
        return 0 if p is None else 1 << p

    def edge_bm(self, types: tuple | None, direction: str) -> int:
        key = (types, direction)
        bm = self._edges.get(key)
        if bm is None:
            pos = self.pos
            rels = list(types) if types else None
            bm = self._from_positions(pos[nid] for nid in self.g.ids_with_edge(rels, direction))
            self._edges[key] = bm
        return bm

    def semi_bm(self, types: tuple | None, candidate_dir: str, other_label: str) -> int:
        key = (types, candidate_dir, other_label)
        bm = self._semi.get(key)
        if bm is None:
            g, pos = self.g, self.pos
            rels = list(types) if types else None
            walk = _INV[candidate_dir]  # from the labelled endpoint toward the candidate
            hits: set[int] = set()
            for oid in g.ids_with_label(other_label):
                for nb in g.neighbours(oid, rels, walk):
                    hits.add(pos[nb])
            bm = self._from_positions(hits)
            self._semi[key] = bm
        return bm


class BitmapExecutor:
    """Bitmap-lowered execution with transparent fallback to the planner."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.g = graph
        self.idx = BitmapIndex(graph)
        self._fallback: OptimizedExecutor | None = None
        self.last_path: str | None = None  # "bitmap" | "fallback", for tests

    def run(self, query: ast.Query) -> list[dict[str, Any]]:
        rows = self._try_bitmap(query)
        if rows is not None:
            self.last_path = "bitmap"
            return rows
        self.last_path = "fallback"
        if self._fallback is None:
            self._fallback = OptimizedExecutor(self.g)
        return self._fallback.run(query)

    # -- the lowered path ---------------------------------------------------

    def _try_bitmap(self, query: ast.Query) -> list[dict[str, Any]] | None:
        if query.order_by:
            return None
        if len(query.match) != 1:
            return None
        path = query.match[0]
        if path.rels or len(path.nodes) != 1:
            return None
        np = path.nodes[0]
        if np.props or not np.var:
            return None
        var = np.var
        for item in query.returns:
            e = item.expr
            if isinstance(e, ast.Variable) and e.name == var:
                continue
            if isinstance(e, ast.Property) and e.var == var:
                continue
            if isinstance(e, ast.Literal):
                continue
            return None

        bm = self.idx.label_bm(np.label)
        if query.where is not None:
            lowered = self._lower(query.where, var)
            if lowered is None:
                return None
            bm &= lowered

        rows = self._materialize(bm, query)
        if query.distinct:
            rows = _dedupe(rows)
        if query.limit is not None:
            rows = rows[: query.limit]
        return rows

    def _materialize(self, bm: int, query: ast.Query) -> list[dict[str, Any]]:
        """Set bits -> rows, in load order (identical to the planner's bucket
        order). One C conversion, then a byte-skip loop."""
        g, ids = self.g, self.idx.ids
        items = query.returns
        rows: list[dict[str, Any]] = []
        data = bm.to_bytes((self.idx.nbits + 7) // 8, "little")
        for byte_i, byte in enumerate(data):
            base = byte_i << 3
            while byte:
                low = byte & -byte
                nid = ids[base + low.bit_length() - 1]
                byte ^= low
                row: dict[str, Any] = {}
                for item in items:
                    e = item.expr
                    if isinstance(e, ast.Variable):
                        row[item.alias] = nid
                    elif isinstance(e, ast.Property):
                        if e.key == "id":
                            row[item.alias] = nid
                        elif e.key == "type":
                            row[item.alias] = g.node(nid).label
                        else:
                            row[item.alias] = g.node(nid)[e.key]
                    else:
                        row[item.alias] = e.value
                rows.append(row)
        return rows

    # -- WHERE lowering ------------------------------------------------------

    def _lower(self, expr, var: str) -> int | None:
        idx = self.idx
        if isinstance(expr, ast.BoolOp):
            out: int | None = None
            for o in expr.operands:
                p = self._lower(o, var)
                if p is None:
                    return None
                if out is None:
                    out = p
                elif expr.op == "AND":
                    out &= p
                else:
                    out |= p
            return out
        if isinstance(expr, ast.Not):
            p = self._lower(expr.operand, var)
            return None if p is None else idx.universe & ~p
        if isinstance(expr, ast.IsNull):
            op = expr.operand
            if not (isinstance(op, ast.Property) and op.var == var):
                return None
            null_bm = 0 if op.key in ("id", "type") else idx.attr_bm(op.key, None)
            return idx.universe & ~null_bm if expr.negated else null_bm
        if isinstance(expr, ast.Comparison):
            return self._lower_cmp(expr, var)
        if isinstance(expr, ast.PatternPredicate):
            return self._lower_pattern(expr.path, var)
        return None

    def _lower_cmp(self, expr, var: str) -> int | None:
        left, right = expr.left, expr.right
        if isinstance(left, ast.Literal) and isinstance(right, ast.Property):
            left, right = right, left
        if not (
            isinstance(left, ast.Property)
            and left.var == var
            and isinstance(right, ast.Literal)
        ):
            return None
        val = right.value
        if expr.op == "IN":
            if not isinstance(val, (list, tuple)):
                return None
            if any(not isinstance(e, _HASHABLE) for e in val):
                return None
            out = 0
            for e in val:
                if e is not None:  # engine semantics: `a IN c` is False for null a
                    out |= self._eq_bm(left.key, e)
            return out
        if not isinstance(val, _HASHABLE):
            return None
        eq = self._eq_bm(left.key, val)
        if expr.op == "=":
            return eq
        if expr.op == "<>":
            return self.idx.universe & ~eq
        return None  # ordered comparisons: fall back

    def _eq_bm(self, key: str, value: Any) -> int:
        if key == "type":
            return self.idx.label_bm(value) if isinstance(value, str) else 0
        if key == "id":
            return self.idx.id_bm(value)
        return self.idx.attr_bm(key, value)

    def _lower_pattern(self, path, var: str) -> int | None:
        if len(path.nodes) != 2 or len(path.rels) != 1:
            return None
        rel = path.rels[0]
        if rel.min_hops != 1 or rel.max_hops != 1:
            return None
        n0, n1 = path.nodes
        if n0.props or n1.props:
            return None
        if (n0.var == var) == (n1.var == var):  # var on neither or both ends
            return None
        pos = 0 if n0.var == var else 1
        other = path.nodes[1 - pos]
        # direction as seen from the candidate node
        cand_dir = rel.direction if pos == 0 else _INV[rel.direction]
        types = tuple(sorted(rel.types)) if rel.types else None
        if other.label is None:
            bm = self.idx.edge_bm(types, cand_dir)
        else:
            bm = self.idx.semi_bm(types, cand_dir, other.label)
        np_c = path.nodes[pos]
        if np_c.label is not None:
            bm &= self.idx.label_bm(np_c.label)
        return bm
