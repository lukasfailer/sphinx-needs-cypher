"""The OSS reference backend: parse + execute Cypher in pure Python.

This is the evaluator behind the idea "ship the language, keep the
engine." It is deliberately *not* the fast incremental engine — it is a correct,
readable evaluator that a maintainer can reason about and that gives an
open-source project a real Cypher surface without depending on the commercial
binary. It is fast enough for the scale where a Sphinx build is already the
bottleneck (a few thousand needs), and honest about being O(matches) rather than
a query planner over a persistent index.
"""

from __future__ import annotations

from typing import Any

from ..graph import PropertyGraph
from ..cypher.parser import parse
from ..cypher.executor import Executor


class ReferenceBackend:
    name = "reference"

    def __init__(self, graph: PropertyGraph) -> None:
        self._exec = Executor(graph)

    @classmethod
    def optimized(cls, graph: PropertyGraph) -> "ReferenceBackend":
        """Same language, same semantics, planned execution — the
        ``OptimizedExecutor`` (pushdown, decorrelation, compiled WHERE)."""
        from ..cypher.optimized import OptimizedExecutor

        be = cls.__new__(cls)
        be._exec = OptimizedExecutor(graph)
        return be

    @classmethod
    def bitmap(cls, graph: PropertyGraph) -> "ReferenceBackend":
        """Same language, same semantics, bitmap-lowered execution — interned
        integer ids + big-int bitmaps (``BitmapExecutor``), with transparent
        fallback to the planned executor for unsupported query shapes."""
        from ..cypher.bitmap import BitmapExecutor

        be = cls.__new__(cls)
        be._exec = BitmapExecutor(graph)
        return be

    def query(self, cypher: str) -> list[dict[str, Any]]:
        return self._exec.run(parse(cypher))

    def select_ids(self, cypher: str, var: str = "n") -> list[str]:
        """Convenience for directive selection: return the ids bound to ``var``."""
        rows = self.query(cypher)
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            nid = row.get(var) or next(iter(row.values()), None)
            if isinstance(nid, str) and nid not in seen:
                seen.add(nid)
                out.append(nid)
        return out
