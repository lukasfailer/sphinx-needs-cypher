"""The shared query suite.

Each entry is a realistic node-selection a ``needtable`` / ``needflow`` would
perform, expressed in the Cypher subset. The same list drives:

* the parity test (reference backend vs. the ``ubc`` engine — identical rows),
* the examples (Python-filter equivalent shown side by side), and
* the benchmark (timed at increasing scale).

Keeping one source of truth means a query that is demonstrated is also the query
that is tested and benchmarked — no divergence between the slides and the code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Q:
    key: str
    title: str
    cypher: str
    bind: str  # the RETURN variable that carries the selected need id
    note: str


SUITE: list[Q] = [
    Q(
        key="untraced_swreqs",
        title="Software requirements with no incoming trace link",
        cypher="MATCH (r:swreq) WHERE NOT ( ()-->(r) ) RETURN r.id",
        bind="r",
        note="The gap finding. A pattern predicate over incoming edges — "
        "something filter_string cannot express, because it sees one need at a "
        "time and never the edge into it.",
    ),
    Q(
        key="asil_d_safety_goals",
        title="Safety goals rated ASIL D",
        cypher="MATCH (s:safety_goal) WHERE s.asil = 'D' RETURN s.id",
        bind="s",
        note="A flat attribute filter. This is the ONE axis where Cypher and "
        "Python are genuinely equivalent — say so plainly, don't overclaim.",
    ),
    Q(
        key="sysreq_to_asil_d",
        title="System requirements tracing (2 hops) to an ASIL-D safety goal",
        cypher=(
            "MATCH (sr:sysreq)-[:implements]->(:fsr)-[:derives_from]->(s:safety_goal) "
            "WHERE s.asil = 'D' RETURN sr.id"
        ),
        bind="sr",
        note="A fixed-length join across three node types (sysreq -> fsr -> "
        "safety_goal). filter_string cannot join; the Python alternative is a "
        "hand-written nested loop in filter_code — the O(n^2) footgun "
        "names.",
    ),
    Q(
        key="unverified_swreqs",
        title="Software requirements not verified by any test",
        cypher=(
            "MATCH (r:swreq) WHERE NOT ( (r)<-[:links|specs]-(:test) ) RETURN r.id"
        ),
        bind="r",
        note="An anti-join across types with a typed edge set: swreqs that no "
        "test reaches by a verifying link. Declarative negation vs. a manual "
        "set-difference maintained by hand in Python.",
    ),
    Q(
        key="hazard_trace_closure",
        title="Everything that traces up to a hazard (full downstream tree)",
        cypher="MATCH (h:hazard)<-[*1..]-(n) WHERE h.id = 'HAZ_TRAJ_DEV' RETURN n.id",
        bind="n",
        note="Variable-length traversal / transitive closure — the complete set "
        "of needs that trace to one hazard. Impossible in filter_string; in "
        "filter_code a hand-rolled BFS that must carry its own visited-set or "
        "loop forever on a cyclic graph.",
    ),
]

BY_KEY = {q.key: q for q in SUITE}
