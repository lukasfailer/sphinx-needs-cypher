"""Tests for incremental index persistence (``needquery.incremental``).

The contract under test: for ANY sequence of mutations to a ``needs.json``,
loading through :class:`IncrementalGraphStore` (which applies deltas to a
cached, previously-built index) must produce a graph indistinguishable from a
fresh full build on the same file — same nodes, same label buckets, same typed
adjacency in both directions, same rel types.

"Indistinguishable" is order-insensitive: openCypher gives no result ordering
without ORDER BY, and the graph's bucket/adjacency lists are semantically
multisets. The comparison helper canonicalises (sorts lists, prunes empty
entries created by defaultdict reads) before comparing.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))

import pytest

from needquery import PropertyGraph, ReferenceBackend
from needquery.incremental import IncrementalGraphStore


# -- helpers ---------------------------------------------------------------


def canonical(g: PropertyGraph) -> dict:
    """Order-insensitive, empty-pruned snapshot of every public index
    structure. Two graphs with equal canonical forms answer every query in the
    supported subset identically (up to row order, which openCypher does not
    define without ORDER BY)."""
    nodes = {
        nid: (n.label, dict(n.attrs)) for nid, n in ((x.id, x) for x in g.nodes())
    }
    by_label = {
        lab: sorted(g.ids_with_label(lab))
        for lab in {n.label for n in g.nodes()}
    }
    adjacency: dict = {}
    for rel in g.rel_types:
        out = {
            nid: sorted(g.out(nid, [rel]))
            for nid in nodes
            if list(g.out(nid, [rel]))
        }
        inc = {
            nid: sorted(g.into(nid, [rel]))
            for nid in nodes
            if list(g.into(nid, [rel]))
        }
        if out or inc:
            adjacency[rel] = (out, inc)
    any_out = {nid: sorted(g.out(nid, None)) for nid in nodes if list(g.out(nid, None))}
    any_in = {nid: sorted(g.into(nid, None)) for nid in nodes if list(g.into(nid, None))}
    return {
        "nodes": nodes,
        "by_label": by_label,
        "rel_types": set(g.rel_types),
        "adjacency": adjacency,
        "any_out": any_out,
        "any_in": any_in,
    }


def assert_graphs_equal(got: PropertyGraph, expected: PropertyGraph) -> None:
    c_got, c_exp = canonical(got), canonical(expected)
    assert c_got["nodes"] == c_exp["nodes"]
    assert c_got["by_label"] == c_exp["by_label"]
    assert c_got["rel_types"] == c_exp["rel_types"]
    assert c_got["adjacency"] == c_exp["adjacency"]
    assert c_got["any_out"] == c_exp["any_out"]
    assert c_got["any_in"] == c_exp["any_in"]


def schema_for(link_fields) -> dict:
    return {"properties": {f: {"field_type": "links"} for f in link_fields}}


def write_needs(path, needs, link_fields=("links", "depends_on", "verifies")):
    doc = {
        "current_version": "1.0",
        "project": "test",
        "versions": {
            "1.0": {"needs": needs, "needs_schema": schema_for(link_fields)}
        },
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def base_needs() -> dict:
    #  A(req) -links-> B(swreq) -links-> C(swreq)
    #  D(swreq) -depends_on-> D (self cycle)
    #  T(test) -verifies-> B, C
    return {
        "A": {"type": "req", "status": "open", "links": ["B"]},
        "B": {"type": "swreq", "status": "open", "links": ["C"]},
        "C": {"type": "swreq", "status": "closed"},
        "D": {"type": "swreq", "status": "open", "depends_on": ["D"]},
        "T": {"type": "test", "status": "open", "verifies": ["B", "C"]},
    }


@pytest.fixture()
def paths(tmp_path):
    return tmp_path / "needs.json", tmp_path / "index.cache"


# -- cold build & fast path ------------------------------------------------


def test_cold_build_creates_cache_and_matches_fresh(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert cache.exists()
    assert store.last_stats["mode"] == "full"
    assert store.last_stats["rebuilt"] == len(base_needs())
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_noop_reload_rebuilds_nothing(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats["mode"] == "noop"
    assert store.last_stats["rebuilt"] == 0
    assert store.last_stats["added"] == 0
    assert store.last_stats["removed"] == 0
    assert store.last_stats["changed"] == 0
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


# -- single-delta cases ----------------------------------------------------


def test_add_need(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    needs["E"] = {"type": "test", "status": "open", "verifies": ["C", "D"]}
    write_needs(needs_json, needs)

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats == {
        "mode": "incremental", "added": 1, "removed": 0, "changed": 0, "rebuilt": 1,
    }
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))
    # the new need's edges exist in BOTH directions
    assert set(g.out("E", ["verifies"])) == {"C", "D"}
    assert "E" in set(g.into("C", ["verifies"]))


def test_add_need_resurrects_dangling_link(paths):
    """A pre-existing need links to an id that does not exist yet (dangling —
    not an edge). When that id is later ADDED, a fresh build has the edge; the
    incremental path must produce it too, even though the *source* need never
    changed."""
    needs_json, cache = paths
    needs = base_needs()
    needs["A"]["links"] = ["B", "FUTURE"]
    write_needs(needs_json, needs)
    g0 = IncrementalGraphStore(cache).load(needs_json)
    assert set(g0.out("A", ["links"])) == {"B"}  # dangling: no edge yet

    needs["FUTURE"] = {"type": "swreq", "status": "open"}
    write_needs(needs_json, needs)
    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats["added"] == 1
    assert store.last_stats["changed"] == 0
    assert set(g.out("A", ["links"])) == {"B", "FUTURE"}
    assert set(g.into("FUTURE", ["links"])) == {"A"}
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_remove_need_retires_edges_both_directions(paths):
    """Removing B must remove A->B from A's outgoing lists, B<-T from T's, and
    B->C from C's incoming list — the other endpoints are untouched needs."""
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    del needs["B"]
    write_needs(needs_json, needs)

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats == {
        "mode": "incremental", "added": 0, "removed": 1, "changed": 0, "rebuilt": 0,
    }
    assert g.node("B") is None
    assert list(g.out("A", ["links"])) == []          # A's out edge retired
    assert set(g.out("T", ["verifies"])) == {"C"}     # T keeps only C
    assert set(g.into("C", ["links"])) == set()       # B->C retired from C's in
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_change_attributes_only(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    needs["C"]["status"] = "open"
    write_needs(needs_json, needs)

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats == {
        "mode": "incremental", "added": 0, "removed": 0, "changed": 1, "rebuilt": 1,
    }
    assert g.node("C")["status"] == "open"
    # edges into C (from B and T) survived untouched
    assert set(g.into("C", ["links"])) == {"B"}
    assert set(g.into("C", ["verifies"])) == {"T"}
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_change_links_retires_old_edges_first(paths):
    """The edge-retirement case: B's link moves from C to D. The old B->C edge
    must vanish from C's incoming list; the new B->D edge must appear in D's
    incoming list; B's incoming edges (A->B, T-verifies->B) must survive."""
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    needs["B"]["links"] = ["D"]
    write_needs(needs_json, needs)

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats["changed"] == 1
    assert set(g.out("B", ["links"])) == {"D"}
    assert set(g.into("C", ["links"])) == set()        # old edge retired
    assert set(g.into("D", ["links"])) == {"B"}        # new edge in reverse index
    assert set(g.into("B", ["links"])) == {"A"}        # incoming edges preserved
    assert set(g.into("B", ["verifies"])) == {"T"}
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_change_label_moves_bucket(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    needs["C"]["type"] = "req"
    write_needs(needs_json, needs)

    g = IncrementalGraphStore(cache).load(needs_json)
    assert "C" in g.ids_with_label("req")
    assert "C" not in g.ids_with_label("swreq")
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_link_field_set_change_invalidates_cache(paths):
    """The set of link-field names is part of how EVERY need's edges were
    extracted — when it changes, the delta path is unsound and the store must
    fall back to a full build (still equal to a fresh one)."""
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    needs["X"] = {"type": "impl", "status": "open", "implements": ["B"]}
    write_needs(needs_json, needs,
                link_fields=("links", "depends_on", "verifies", "implements"))

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats["mode"] == "full"
    assert set(g.into("B", ["implements"])) == {"X"}
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_file_shrinks_to_empty(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    write_needs(needs_json, {})
    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert len(g) == 0
    assert list(g.nodes()) == []
    assert g.rel_types == frozenset()
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))


def test_corrupted_cache_falls_back_to_full_build(paths):
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)
    cache.write_bytes(b"\x80\x04 this is not a cache")

    store = IncrementalGraphStore(cache)
    g = store.load(needs_json)
    assert store.last_stats["mode"] == "full"
    assert_graphs_equal(g, PropertyGraph.from_needs_json(needs_json))
    # and the rewritten cache is healthy again
    store2 = IncrementalGraphStore(cache)
    store2.load(needs_json)
    assert store2.last_stats["mode"] == "noop"


def test_remove_then_readd_round_trip(paths):
    """Delete a need, reload, re-add the identical need, reload — the dangling
    bookkeeping must bring back exactly the fresh-build edge set."""
    needs_json, cache = paths
    write_needs(needs_json, base_needs())
    IncrementalGraphStore(cache).load(needs_json)

    needs = base_needs()
    del needs["C"]
    write_needs(needs_json, needs)
    g1 = IncrementalGraphStore(cache).load(needs_json)
    assert_graphs_equal(g1, PropertyGraph.from_needs_json(needs_json))

    needs = base_needs()  # C is back, byte-identical
    write_needs(needs_json, needs)
    store = IncrementalGraphStore(cache)
    g2 = store.load(needs_json)
    assert store.last_stats["added"] == 1
    assert set(g2.into("C", ["links"])) == {"B"}
    assert set(g2.into("C", ["verifies"])) == {"T"}
    assert_graphs_equal(g2, PropertyGraph.from_needs_json(needs_json))


# -- invariant over a mutation sequence ------------------------------------


def test_mutation_sequence_invariant(paths):
    """A multi-round add/remove/change churn; after every round the
    incrementally-maintained graph must equal a fresh full build."""
    needs_json, cache = paths
    needs = base_needs()
    write_needs(needs_json, needs)
    store = IncrementalGraphStore(cache)
    store.load(needs_json)

    rounds = [
        # (description, mutation)
        ("add two, one linking the other",
         lambda n: n.update({
             "E": {"type": "impl", "status": "open", "links": ["F"]},
             "F": {"type": "swreq", "status": "open"},
         })),
        ("change links + attrs on same need",
         lambda n: n["B"].update({"links": ["D", "F"], "status": "closed"})),
        ("remove a hub with in+out edges",
         lambda n: n.pop("B")),
        ("self-link and duplicate targets",
         lambda n: n.update({"G": {"type": "swreq", "status": "open",
                                   "depends_on": ["G", "D", "D"]}})),
        ("remove everything but one",
         lambda n: [n.pop(k) for k in list(n) if k != "D"]),
        ("repopulate",
         lambda n: n.update(base_needs())),
    ]
    for desc, mutate in rounds:
        mutate(needs)
        write_needs(needs_json, needs)
        g = IncrementalGraphStore(cache).load(needs_json)
        fresh = PropertyGraph.from_needs_json(needs_json)
        try:
            assert_graphs_equal(g, fresh)
        except AssertionError as e:
            raise AssertionError(f"divergence after round: {desc}") from e


# -- query parity on a realistic graph -------------------------------------


def test_query_parity_incremental_vs_fresh(paths):
    """Both executors must return identical result sets on an incrementally
    loaded graph and a freshly built one, using the shared query suite."""
    import generate  # bench/generate.py, added to sys.path above
    from queries import SUITE

    needs_json, cache = paths
    doc = generate.make_graph(60, seed=7)
    needs_json.write_text(json.dumps(doc), encoding="utf-8")
    IncrementalGraphStore(cache).load(needs_json)

    needs = doc["versions"]["1.0"]["needs"]
    needs["SWREQ_3"]["status"] = "closed"           # attr change
    needs["SWREQ_3"]["links"] = ["REQ_1"]           # edge change
    del needs["TEST_5"]                             # removal
    needs["SWREQ_NEW"] = {"id": "SWREQ_NEW", "type": "swreq", "title": "n",
                          "status": "open", "links": ["REQ_2"]}
    needs_json.write_text(json.dumps(doc), encoding="utf-8")

    store = IncrementalGraphStore(cache)
    g_inc = store.load(needs_json)
    assert store.last_stats["mode"] == "incremental"
    g_fresh = PropertyGraph.from_needs_json(needs_json)
    assert_graphs_equal(g_inc, g_fresh)

    def rows_sorted(backend, q):
        return sorted(json.dumps(r, sort_keys=True) for r in backend.query(q))

    for q in SUITE:
        for make in (ReferenceBackend, ReferenceBackend.optimized):
            assert rows_sorted(make(g_inc), q.cypher) == \
                rows_sorted(make(g_fresh), q.cypher), q.key
