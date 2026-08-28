"""In-memory property graph over a Sphinx-Needs project.

A Sphinx-Needs project *is* a property graph: every need is a node with a unique
id, typed scalar attributes, and typed links to other needs. This module loads
that graph once from a ``needs.json`` and builds the indexes a query planner
needs so that node selection is a graph traversal instead of an O(needs) Python
scan repeated per directive.

Design notes
------------
* **Link fields are read from ``needs_schema``, not guessed.** Sphinx-Needs
  stamps every field with a ``field_type`` (``links`` / ``backlinks`` / ``core``
  / ``extra``). We treat ``links`` fields as directed edges and ignore
  ``backlinks`` (they are the reverse of an edge we already hold; keeping both
  would double-count). If the schema is absent we fall back to a conservative
  heuristic.
* **Every index is built once, at load.** Node-by-id, label buckets, and typed
  adjacency (forward *and* reverse) are materialised so the executor never scans
  all nodes to answer ``(a)-[:rel]->(b)``. This is the structural reason the
  declarative path can beat a per-need Python ``eval``: the work is done once and
  reused, not redone for every directive on the page.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


@dataclass(slots=True)
class Node:
    """A single need. ``attrs`` holds only scalar/document fields; edges live in
    the graph's adjacency indexes, never on the node, so traversal never touches
    a Python dict lookup per hop for the *set* of neighbours."""

    id: str
    label: str  # the need type, e.g. "swreq", "req", "test"
    attrs: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.attrs.get(key)


class PropertyGraph:
    """Indexed property graph. Build once, query many times."""

    __slots__ = (
        "_nodes",
        "_by_label",
        "_out",
        "_in",
        "_rel_types",
        "_out_any",
        "_in_any",
    )

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._by_label: dict[str, list[str]] = defaultdict(list)
        # (rel_type) -> {src_id -> [dst_id, ...]}
        self._out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self._in: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self._rel_types: set[str] = set()
        # relationship-agnostic adjacency, precomputed for the common
        # "does this node have ANY incoming/outgoing edge" existence check.
        self._out_any: dict[str, set[str]] = defaultdict(set)
        self._in_any: dict[str, set[str]] = defaultdict(set)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_needs_json(cls, path: str | Path) -> "PropertyGraph":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = raw["versions"][raw["current_version"]]
        needs: dict[str, dict] = version["needs"]
        link_fields = cls._link_fields(version.get("needs_schema"), needs)
        return cls.from_needs(needs, link_fields)

    @classmethod
    def from_needs(
        cls, needs: Mapping[str, Mapping[str, Any]], link_fields: Iterable[str]
    ) -> "PropertyGraph":
        g = cls()
        link_fields = set(link_fields)
        ids = set(needs)
        for nid, data in needs.items():
            g._add_node(nid, str(data.get("type", "need")), data)
        for nid, data in needs.items():
            for rel in link_fields:
                targets = data.get(rel)
                if not targets:
                    continue
                for tgt in targets:
                    if tgt in ids:  # skip dangling links; they are not edges
                        g._add_edge(nid, rel, tgt)
        return g

    def _add_node(self, nid: str, label: str, attrs: Mapping[str, Any]) -> None:
        self._nodes[nid] = Node(nid, label, attrs)
        self._by_label[label].append(nid)

    def _add_edge(self, src: str, rel: str, dst: str) -> None:
        self._out[rel][src].append(dst)
        self._in[rel][dst].append(src)
        self._rel_types.add(rel)
        self._out_any[src].add(dst)
        self._in_any[dst].add(src)

    @staticmethod
    def _link_fields(schema: Any, needs: Mapping[str, Mapping]) -> set[str]:
        """Authoritative when ``needs_schema`` is present; heuristic otherwise."""
        if isinstance(schema, Mapping) and "properties" in schema:
            return {
                name
                for name, spec in schema["properties"].items()
                if isinstance(spec, Mapping) and spec.get("field_type") == "links"
            }
        # Fallback: a field whose value, wherever present, is a list of strings
        # that are all valid need ids. Excludes the ``_back`` reverse fields.
        ids = set(needs)
        candidates: set[str] = set()
        rejected: set[str] = set()
        for data in needs.values():
            for k, v in data.items():
                if k.endswith("_back") or k in rejected:
                    continue
                if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    if all(x in ids for x in v):
                        candidates.add(k)
                    else:
                        candidates.discard(k)
                        rejected.add(k)
        return candidates

    # -- read API used by the executor ------------------------------------

    def node(self, nid: str) -> Node | None:
        return self._nodes.get(nid)

    def __len__(self) -> int:
        return len(self._nodes)

    def nodes(self) -> Iterator[Node]:
        return iter(self._nodes.values())

    @property
    def rel_types(self) -> frozenset[str]:
        return frozenset(self._rel_types)

    def label_count(self, label: str) -> int:
        return len(self._by_label.get(label, ()))

    def ids_with_label(self, label: str | None) -> list[str]:
        if label is None:
            return list(self._nodes)
        return self._by_label.get(label, [])

    def out(self, nid: str, rels: Iterable[str] | None) -> Iterator[str]:
        """Neighbours reachable from ``nid`` via one forward edge of a given
        type (or any type when ``rels`` is None)."""
        if rels is None:
            yield from self._out_any.get(nid, ())
            return
        for rel in rels:
            yield from self._out[rel].get(nid, ())

    def into(self, nid: str, rels: Iterable[str] | None) -> Iterator[str]:
        if rels is None:
            yield from self._in_any.get(nid, ())
            return
        for rel in rels:
            yield from self._in[rel].get(nid, ())

    def has_out(self, nid: str, rels: Iterable[str] | None) -> bool:
        if rels is None:
            return bool(self._out_any.get(nid))
        return any(self._out[rel].get(nid) for rel in rels)

    def has_in(self, nid: str, rels: Iterable[str] | None) -> bool:
        if rels is None:
            return bool(self._in_any.get(nid))
        return any(self._in[rel].get(nid) for rel in rels)

    def ids_with_edge(self, rels: Iterable[str] | None, direction: str) -> Iterator[str]:
        """All node ids that have at least one edge of the given type(s) in the
        given direction — the build side of a decorrelated existence check,
        served straight from the adjacency indexes instead of an edge sweep."""
        if direction == "both":
            seen: set[str] = set()
            for d in ("out", "in"):
                for nid in self.ids_with_edge(rels, d):
                    if nid not in seen:
                        seen.add(nid)
                        yield nid
            return
        if rels is None:
            yield from (self._out_any if direction == "out" else self._in_any)
            return
        table = self._out if direction == "out" else self._in
        emitted: set[str] = set()
        for rel in rels:
            for nid in table.get(rel, {}):
                if nid not in emitted:
                    emitted.add(nid)
                    yield nid

    def neighbours(
        self, nid: str, rels: Iterable[str] | None, direction: str
    ) -> Iterator[str]:
        if direction == "out":
            yield from self.out(nid, rels)
            return
        if direction == "in":
            yield from self.into(nid, rels)
            return
        # undirected: both ways, de-duplicated
        seen: set[str] = set()
        for x in self.out(nid, rels):
            if x not in seen:
                seen.add(x)
                yield x
        for x in self.into(nid, rels):
            if x not in seen:
                seen.add(x)
                yield x
