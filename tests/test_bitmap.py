"""Tests for the bitmap executor — the third planner fork.

The bar is bit-identical semantics with the other two executors: every query
either lowers to bitmap operations and returns exactly what the reference
executor returns, or falls back to the planned executor. The `last_path`
attribute makes the chosen path observable, so a silent everything-falls-back
regression cannot pass this suite.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from needquery import PropertyGraph, ReferenceBackend
from needquery.cypher.bitmap import BitmapExecutor
from needquery.cypher.parser import parse

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "needs.ubc.json")


def tiny_graph() -> PropertyGraph:
    needs = {
        "A": {"type": "req", "status": "open", "links": ["B"]},
        "B": {"type": "swreq", "status": "open"},
        "C": {"type": "swreq", "status": "closed"},
        "D": {"type": "swreq", "status": "open", "depends_on": ["D"]},
        "T": {"type": "test", "links": ["C"], "verifies": ["B"]},
    }
    return PropertyGraph.from_needs(needs, {"links", "verifies", "depends_on"})


def both(graph: PropertyGraph, cypher: str):
    """(bitmap rows, reference rows) for the same query."""
    ref = ReferenceBackend(graph).query(cypher)
    ex = BitmapExecutor(graph)
    got = ex.run(parse(cypher))
    return ex, got, ref


def rows_set(rows):
    return {tuple(sorted(r.items())) for r in rows}


# -- lowered shapes must match the reference AND actually use bitmaps -------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (r:swreq) WHERE r.status = 'open' RETURN r",
        "MATCH (r:swreq) WHERE r.status = 'open' RETURN r.id",
        "MATCH (r:swreq) WHERE NOT ( ()-->(r) ) RETURN r",
        "MATCH (r:swreq) WHERE NOT ( (r)<-[:links]-(:test) ) RETURN r",
        "MATCH (r:swreq) WHERE ( (r)<-[:verifies]-(:test) ) RETURN r",
        "MATCH (r:swreq) WHERE r.status = 'open' OR NOT r.status = 'closed' RETURN r",
        "MATCH (r:swreq) WHERE r.status <> 'closed' RETURN r",
        "MATCH (r) WHERE r.status = 'open' RETURN r",
        "MATCH (r:swreq) WHERE r.id = 'B' RETURN r",
        "MATCH (r:swreq) WHERE r.type = 'swreq' RETURN r",
    ],
)
def test_bitmap_path_matches_reference(cypher):
    ex, got, ref = both(tiny_graph(), cypher)
    assert ex.last_path == "bitmap"
    assert rows_set(got) == rows_set(ref)


def test_missing_attribute_semantics():
    g = tiny_graph()
    # equality with a missing attribute: no rows
    ex, got, ref = both(g, "MATCH (r:swreq) WHERE r.missing = 'x' RETURN r")
    assert ex.last_path == "bitmap"
    assert rows_set(got) == rows_set(ref) == set()
    # inequality with a missing attribute: engine semantics say all rows
    ex, got, ref = both(g, "MATCH (r:swreq) WHERE r.missing <> 'x' RETURN r")
    assert ex.last_path == "bitmap"
    assert rows_set(got) == rows_set(ref)
    assert len(got) == 3
    # IS NULL / IS NOT NULL
    ex, got, ref = both(g, "MATCH (r:swreq) WHERE r.missing IS NULL RETURN r")
    assert ex.last_path == "bitmap"
    assert rows_set(got) == rows_set(ref)
    ex, got, ref = both(g, "MATCH (r:swreq) WHERE r.status IS NOT NULL RETURN r")
    assert ex.last_path == "bitmap"
    assert rows_set(got) == rows_set(ref)


def test_row_order_matches_optimized():
    """Same order as the planned executor (bucket load order), not just the
    same set — directive output is order-sensitive."""
    g = tiny_graph()
    cypher = "MATCH (r:swreq) WHERE r.status = 'open' RETURN r"
    opt = ReferenceBackend.optimized(g).query(cypher)
    ex = BitmapExecutor(g)
    assert ex.run(parse(cypher)) == opt


# -- unsupported shapes must fall back, and still be correct ----------------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (r:swreq) WHERE r.status = 'open' RETURN r ORDER BY r.id",
        "MATCH (sr:req)-[:links]->(b:swreq) RETURN sr",
        "MATCH (h:swreq)<-[*1..]-(n) WHERE h.id = 'B' RETURN n",
        "MATCH (r:swreq) WHERE r.status STARTS WITH 'op' RETURN r",
    ],
)
def test_fallback_path_matches_reference(cypher):
    ex, got, ref = both(tiny_graph(), cypher)
    assert ex.last_path == "fallback"
    assert rows_set(got) == rows_set(ref)


# -- differential over the real graph and the shared suite ------------------


def test_suite_differential_on_demo_graph():
    graph = PropertyGraph.from_needs_json(DATA)
    sys.path.insert(0, os.path.dirname(__file__))
    from queries import SUITE

    ref = ReferenceBackend(graph)
    ex = BitmapExecutor(graph)
    for q in SUITE:
        got = ex.run(parse(q.cypher))
        expected = ref.query(q.cypher)
        assert rows_set(got) == rows_set(expected), q.key
