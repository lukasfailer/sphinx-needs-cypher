"""The shim must be *result-equivalent*, not just syntactically plausible: for a
translatable filter_string, running the emitted Cypher on the demo graph must
select exactly the same needs the original filter_string selects.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

import pytest

from needquery import PropertyGraph, ReferenceBackend
import status_quo as sq
from needquery.shim import translate, Untranslatable

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "needs.ubc.json")

TRANSLATABLE = [
    "type == 'swreq' and status == 'open'",
    "type == 'safety_goal' and asil == 'D'",
    "type == 'swreq'",
    "status == 'open' or status == 'closed'",
    "type == 'req' and not (status == 'closed')",
]

REFUSED = [
    "len(links) == 0",
    "search('ADAS', title)",
    "id.startswith('SWREQ')",
]


@pytest.fixture(scope="module")
def env():
    graph = PropertyGraph.from_needs_json(DATA)
    return ReferenceBackend(graph), sq.load_needs(DATA)


@pytest.mark.parametrize("expr", TRANSLATABLE)
def test_translation_is_result_equivalent(env, expr):
    backend, needs = env
    cypher = translate(expr).cypher
    via_cypher = set(backend.select_ids(cypher))
    via_filter = set(sq.filter_string(needs, expr))
    assert via_cypher == via_filter, f"{expr!r} -> {cypher!r}"


@pytest.mark.parametrize("expr", REFUSED)
def test_unsafe_filters_are_refused_not_mistranslated(expr):
    with pytest.raises(Untranslatable):
        translate(expr)
