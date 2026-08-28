"""Part A, made runnable: for each realistic selection, show the Cypher answer
(reference engine) next to the Python-today answer, and demonstrate the specific
thing that goes wrong on the Python side.

    PYTHONPATH=src python examples/compare.py

Every number printed is computed live from the demo graph. Nothing here is
asserted without being run.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from needquery import PropertyGraph, ReferenceBackend  # noqa: E402
import status_quo as sq  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "needs.ubc.json")


def rule(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main() -> None:
    graph = PropertyGraph.from_needs_json(DATA)
    cy = ReferenceBackend(graph)
    needs = sq.load_needs(DATA)

    # 1 -----------------------------------------------------------------
    rule("1. Untraced software requirements — a pattern predicate over incoming edges")
    cy_ids = set(cy.select_ids("MATCH (r:swreq) WHERE NOT ( ()-->(r) ) RETURN r", "r"))
    # The naive filter_string an author reaches for first: only 'links_back'.
    naive = set(sq.filter_string(needs, "type == 'swreq' and not links_back"))
    # The "correct" filter_string: must enumerate EVERY reverse-link field.
    all_back = " or ".join(f"{f}_back" for f in sq.LINK_FIELDS)
    correct = set(sq.filter_string(needs, f"type == 'swreq' and not ({all_back})"))
    print(f"Cypher (declarative):           {len(cy_ids):>3}  -> {sorted(cy_ids)}")
    print(f"filter_string, naive links_back:{len(naive):>3}  <- WRONG "
          f"(treats {len(naive) - len(cy_ids)} traced reqs as untraced)")
    print(f"filter_string, all 21 _back:    {len(correct):>3}  correct — but only by "
          f"hand-listing every link type; add one and it silently breaks")
    assert cy_ids == correct, "reference engine must equal the exhaustive filter"

    # 2 -----------------------------------------------------------------
    rule("2. ASIL-D safety goals — a flat attribute filter (the honest tie)")
    cy_ids = set(cy.select_ids("MATCH (s:safety_goal) WHERE s.asil='D' RETURN s", "s"))
    fs = set(sq.filter_string(needs, "type == 'safety_goal' and asil == 'D'"))
    print(f"Cypher:        {len(cy_ids):>3}")
    print(f"filter_string: {len(fs):>3}")
    print(f"identical sets: {cy_ids == fs}  -> on a flat filter there is NO advantage. "
          "Say so.")
    assert cy_ids == fs

    # 3 -----------------------------------------------------------------
    rule("3. Sysreq -> fsr -> ASIL-D safety goal — a 2-hop join (filter_string cannot)")
    q = ("MATCH (sr:sysreq)-[:implements]->(:fsr)-[:derives_from]->(s:safety_goal) "
         "WHERE s.asil='D' RETURN sr")
    cy_ids = set(cy.select_ids(q, "sr"))

    def join_naive(needs, results):
        # filter_string can't reach across links, so this HAS to be filter_code.
        # Written the obvious way, it is a triple nested scan: O(sysreq*fsr*sg).
        for sr in needs.values():
            if sr.get("type") != "sysreq":
                continue
            for fsr_id in sr.get("implements") or []:
                fsr = needs.get(fsr_id, {})
                for sg_id in fsr.get("derives_from") or []:
                    sg = needs.get(sg_id, {})
                    if sg.get("type") == "safety_goal" and sg.get("asil") == "D":
                        results.append(sr["id"])
    py_ids = set(sq.filter_code(needs, join_naive))
    print(f"Cypher (declarative join):  {len(cy_ids):>3}")
    print(f"filter_code (hand-rolled):  {len(py_ids):>3}")
    print("filter_string:               -- cannot express a join at all")
    print(f"identical sets: {cy_ids == py_ids}  -> equal RESULT, but the Python is a "
          "nested loop the author maintains and the engine can't optimise")
    assert cy_ids == py_ids

    # 4 -----------------------------------------------------------------
    rule("4. Software requirements no TEST verifies — an anti-join on neighbour TYPE")
    q = "MATCH (r:swreq) WHERE NOT ( (r)<-[:links|specs]-(:test) ) RETURN r"
    cy_ids = set(cy.select_ids(q, "r"))
    print("filter_string cannot express this: it can see r.links_back (who points "
          "at me) but NOT the TYPE of those neighbours, so it cannot ask "
          "'is any of them a test?'. It needs a second lookup = traversal.")

    def unverified(needs, results):
        for r in needs.values():
            if r.get("type") != "swreq":
                continue
            verified = False
            for src in (r.get("links_back") or []) + (r.get("specs_back") or []):
                if needs.get(src, {}).get("type") == "test":
                    verified = True
                    break
            if not verified:
                results.append(r["id"])
    py_ids = set(sq.filter_code(needs, unverified))
    print(f"Cypher:      {len(cy_ids):>3}")
    print(f"filter_code: {len(py_ids):>3}   identical sets: {cy_ids == py_ids}")
    assert cy_ids == py_ids

    # 5 -----------------------------------------------------------------
    rule("5. Full trace tree under a hazard — transitive closure (the cycle trap)")
    q = "MATCH (h:hazard)<-[*1..]-(n) WHERE h.id='HAZ_TRAJ_DEV' RETURN n"
    cy_ids = set(cy.select_ids(q, "n"))

    def closure_safe(needs, results):
        # The filter_code an author must write by hand. Correct ONLY because it
        # carries a visited set; drop it and a cyclic graph loops forever.
        seen: set[str] = set()
        stack = ["HAZ_TRAJ_DEV"]
        # build reverse index once (the author has to know to do this)
        rev: dict[str, list[str]] = {}
        for nid, data in needs.items():
            for f in sq.LINK_FIELDS:
                for tgt in data.get(f) or []:
                    rev.setdefault(tgt, []).append(nid)
        while stack:
            cur = stack.pop()
            for src in rev.get(cur, []):
                if src not in seen:
                    seen.add(src)
                    stack.append(src)
        results.extend(seen)
    py_ids = set(sq.filter_code(needs, closure_safe))
    print(f"Cypher (variable-length -[*1..]-):  {len(cy_ids):>3}")
    print(f"filter_code hand-rolled BFS:        {len(py_ids):>3}   "
          f"identical sets: {cy_ids == py_ids}")
    print("filter_string:                        -- cannot express recursion")
    _demo_cycle_hang()
    assert cy_ids == py_ids

    print("\nAll five scenarios reproduced on the real demo graph. "
          "Cypher == Python where Python can express it; Python cannot express 3/5.")


def _demo_cycle_hang() -> None:
    """Show, on a 3-node cycle, that the naive traversal without a visited set
    never terminates — the exact footgun here ('shoot yourself in the
    foot'). We bound it with a step counter so this stays a demonstration."""
    cyc = {"A": ["B"], "B": ["C"], "C": ["A"]}
    steps = 0
    stack = ["A"]
    while stack and steps < 1000:  # the guard a hand-written filter_code omits
        cur = stack.pop()
        stack.extend(cyc.get(cur, []))
        steps += 1
    print(f"  cycle demo: naive DFS without a visited-set took {steps}+ steps on a "
          "3-node cycle (capped at 1000) — it does not terminate on its own.")


if __name__ == "__main__":
    main()
