"""Unit tests for the reference engine: parser, executor, and the graph model.

These run without ubc and without the demo data — they use a tiny hand-built
graph so each behaviour (label match, directed hop, variable length, cycle
termination, pattern predicate, anti-join) is checked in isolation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from needquery import PropertyGraph, ReferenceBackend
from needquery.cypher.parser import parse, CypherSyntaxError


def tiny_graph() -> PropertyGraph:
    #  A(req) -links-> B(swreq) <-verifies- T(test)
    #  C(swreq) has no verifier.  D(swreq)-depends_on->D  (a self-cycle)
    needs = {
        "A": {"type": "req", "status": "open", "links": ["B"]},
        "B": {"type": "swreq", "status": "open"},
        "C": {"type": "swreq", "status": "closed"},
        "D": {"type": "swreq", "status": "open", "depends_on": ["D"]},
        "T": {"type": "test", "verifies": ["B"]},
    }
    return PropertyGraph.from_needs(needs, {"links", "verifies", "depends_on"})


@pytest.fixture(params=["reference", "optimized"])
def be(request) -> ReferenceBackend:
    """Every engine test runs against BOTH executors — the naive reference
    interpreter and the planned one — so the optimizations can never drift
    from the semantics."""
    if request.param == "optimized":
        return ReferenceBackend.optimized(tiny_graph())
    return ReferenceBackend(tiny_graph())


def test_label_filter(be):
    assert set(be.select_ids("MATCH (n:swreq) RETURN n")) == {"B", "C", "D"}


def test_attribute_filter(be):
    assert set(be.select_ids("MATCH (n:swreq) WHERE n.status = 'open' RETURN n")) == {
        "B", "D"
    }


def test_directed_hop(be):
    assert set(be.select_ids("MATCH (a:req)-[:links]->(n) RETURN n")) == {"B"}


def test_incoming_pattern_predicate(be):
    # swreqs with NO incoming edge of any type: C and D-incoming? D has a self
    # loop so it DOES have an incoming edge; C has none.
    got = set(be.select_ids("MATCH (n:swreq) WHERE NOT ( ()-->(n) ) RETURN n"))
    assert got == {"C"}


def test_anti_join_on_neighbour_type(be):
    # swreqs not verified by any test
    q = "MATCH (n:swreq) WHERE NOT ( (n)<-[:verifies]-(:test) ) RETURN n"
    assert set(be.select_ids(q)) == {"C", "D"}


def test_variable_length_terminates_on_cycle(be):
    # D depends_on D — a self cycle. A closure must terminate and not include a
    # spurious extra node.
    got = be.select_ids("MATCH (s:swreq)-[:depends_on*1..]->(n) WHERE s.id='D' RETURN n")
    assert got == ["D"]


def test_in_operator(be):
    q = "MATCH (n) WHERE n.status IN ['open'] RETURN n"
    assert "B" in set(be.select_ids(q))


def test_syntax_error_on_non_query():
    with pytest.raises(CypherSyntaxError):
        parse("open('/etc/passwd')")


def test_return_projection(be):
    rows = be.query("MATCH (n:swreq) WHERE n.id='B' RETURN n.id AS id, n.status AS s")
    assert rows == [{"id": "B", "s": "open"}]
