"""A faithful, minimal re-implementation of how Sphinx-Needs selects nodes today.

Sphinx-Needs offers four imperative selection mechanisms. The two that matter
for this comparison:

* ``filter_string`` — a Python expression **evaluated per need** with the need's
  own fields as local names, via ``eval()``. It can read the need's own
  attributes and its own link lists, but it sees exactly one need at a time: it
  cannot look at the node a link points to.
* ``filter_code`` — an arbitrary Python block with access to the whole ``needs``
  collection and a ``results`` list it appends to. Anything you can write in
  Python, including graph traversal — which means anything you can get wrong in
  Python, including O(n^2) loops and non-terminating walks on cyclic data.

This module reproduces both faithfully enough to run the real thing on the real
demo graph, so the "what goes wrong" claims are demonstrated, not asserted.

Back-links: the live Sphinx build computes reverse-link fields (``links_back``,
``implements_back`` …). The ``ubc`` JSON export omits them, so we recompute them
here exactly the way Sphinx-Needs does. That keeps the Python path *fair* — it is
given the same information a real ``needtable`` would have.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


# The link option names in the demo project's ubproject.toml. A filter_string
# author who wants "no incoming link" has to know and enumerate this exact list —
# and keep it in sync as the schema grows. That coupling is the whole point.
LINK_FIELDS = [
    "links", "parent_needs", "author", "based_on", "implements", "depends_on",
    "realizes", "spec", "specs", "runs", "mitigates", "reqs", "derives_from",
    "consumes", "provides", "provided_by", "uses", "startup_calls",
    "shutdown_calls", "release", "persons",
]


def load_needs(path: str | Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = raw["versions"][raw["current_version"]]
    needs = {nid: dict(data) for nid, data in version["needs"].items()}
    _add_back_links(needs)
    return needs


def _add_back_links(needs: dict[str, dict[str, Any]]) -> None:
    """Recompute ``<link>_back`` fields the way Sphinx-Needs does at build time."""
    backs: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    ids = set(needs)
    for nid, data in needs.items():
        for field in LINK_FIELDS:
            for tgt in data.get(field) or []:
                if tgt in ids:
                    backs[tgt][f"{field}_back"].append(nid)
    for nid, data in needs.items():
        for field in LINK_FIELDS:
            data.setdefault(f"{field}_back", backs.get(nid, {}).get(f"{field}_back", []))


# -- filter_string: the real eval() mechanism -----------------------------

def filter_string(needs: dict[str, dict], expr: str) -> list[str]:
    """Select ids whose need makes ``expr`` truthy. This is ``eval(expr, ...,
    need)`` per need — the actual Sphinx-Needs semantics, including the fact that
    the need's attributes are injected as local variables."""
    selected: list[str] = []
    safe_globals = {"__builtins__": {"len": len, "any": any, "all": all, "str": str}}
    for nid, need in needs.items():
        try:
            if eval(expr, safe_globals, need):  # noqa: S307 — this IS the point
                selected.append(nid)
        except Exception:
            # Sphinx-Needs logs and skips; a per-need eval error silently drops
            # the need from the result — another failure mode of the approach.
            continue
    return selected


# -- filter_code: arbitrary Python over the whole collection --------------

def filter_code(needs: dict[str, dict], fn: Callable[[dict, list], None]) -> list[str]:
    """Run a filter_code-style block: it receives ``needs`` and a ``results``
    list to append ids to."""
    results: list[str] = []
    fn(needs, results)
    return results
