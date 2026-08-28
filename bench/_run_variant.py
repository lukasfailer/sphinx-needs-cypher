"""Run all benchmark workloads for ONE engine variant in ONE process.

Called by ``bench/benchmark.py`` as a subprocess so each variant gets a cold,
isolated interpreter — in particular so the Cython-compiled package (imported
via PYTHONPATH=build/cython) can never leak into the pure-Python measurement.

Prints a JSON document to stdout:

    {"variant": ..., "compiled": bool, "load_ms": float,
     "workloads": {"A_flat": {"impls": {"filter_string": {"runs": [...],
                                        "best": ..., "median": ..., "count": N}}}}}

Variants:
    python     — the imperative status quo (filter_string / filter_code)
    reference  — the naive pure-Python Cypher interpreter
    optimized  — the planned executor (pushdown, decorrelation, compiled WHERE);
                 reports "compiled": true when the Cython build is on the path
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from statistics import median

# A workload whose single run exceeds this is not repeated — the point is made.
MAX_REPEAT_MS = 3000.0

CYPHER = {
    "A_flat": ("MATCH (r:swreq) WHERE r.status = 'open' RETURN r", "r"),
    "B_antijoin": ("MATCH (r:swreq) WHERE NOT ( (r)<-[:links]-(:test) ) RETURN r", "r"),
    "C_join": (
        "MATCH (sr:sysreq)-[:implements]->(f:fsr)-[:derives_from]->(s:safety_goal) "
        "WHERE s.asil = 'D' RETURN sr",
        "sr",
    ),
    "D_closure": ("MATCH (h:safety_goal)<-[*1..]-(n) WHERE h.id = 'SG_0' RETURN n", "n"),
    # E. one query that is a join AND two attribute predicates AND an
    # anti-join — the shape a real "which closed swreqs against an open req
    # are still untested?" review question has.
    "E_complex": ("MATCH (sr:swreq)-[:links]->(r:req) WHERE sr.status = 'open' AND r.status = 'open' AND NOT ( (sr)<-[:implements]-(:impl) ) RETURN sr", "sr"),
}


def timed(fn, repeat: int):
    runs = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        runs.append((time.perf_counter() - t0) * 1000.0)
        if runs[0] > MAX_REPEAT_MS:
            break
    return runs, out


def measure(fn, repeat: int):
    runs, out = timed(fn, repeat)
    return {
        "runs": [round(r, 3) for r in runs],
        "best": round(min(runs), 3),
        "median": round(median(runs), 3),
        "count": len(out) if out is not None else None,
    }, out


# -- the imperative status quo -------------------------------------------


def run_python(graph_path: str, repeat: int) -> dict:
    import status_quo as sq  # examples/status_quo.py, via PYTHONPATH

    t0 = time.perf_counter()
    needs = sq.load_needs(graph_path)  # includes back-link computation
    load_ms = (time.perf_counter() - t0) * 1000.0

    wl: dict = {}

    # A. flat filter — the honest pair: eval-per-need vs the AST-fast-path
    # equivalent (a direct-access loop, what sphinx-needs PR #1677 compiles to).
    a_eval, r_eval = measure(
        lambda: set(sq.filter_string(needs, "type == 'swreq' and status == 'open'")),
        repeat,
    )
    a_fast, r_fast = measure(
        lambda: {
            nid
            for nid, d in needs.items()
            if d.get("type") == "swreq" and d.get("status") == "open"
        },
        repeat,
    )
    assert r_eval == r_fast
    wl["A_flat"] = {"impls": {"filter_string_eval": a_eval, "ast_fastpath": a_fast}}

    # B. anti-join ("unverified swreqs") — author-style O(N^2) scan vs the
    # reverse index an expert builds by hand.
    def author_scan():
        out = set()
        for r in needs.values():
            if r.get("type") != "swreq":
                continue
            verified = False
            for other in needs.values():  # "iterate 30,000 needs by hand"
                if other.get("type") == "test" and r["id"] in (other.get("links") or []):
                    verified = True
                    break
            if not verified:
                out.add(r["id"])
        return out

    def expert_index():
        has_test = set()
        for other in needs.values():
            if other.get("type") == "test":
                for tgt in other.get("links") or []:
                    has_test.add(tgt)
        return {
            r["id"]
            for r in needs.values()
            if r.get("type") == "swreq" and r["id"] not in has_test
        }

    b_scan, r_scan = measure(author_scan, repeat)
    b_idx, r_idx = measure(expert_index, repeat)
    assert r_scan == r_idx
    wl["B_antijoin"] = {"impls": {"author_scan": b_scan, "expert_index": b_idx}}

    # C. forward 2-hop join — follows forward link fields, which Python does
    # natively and cheaply. Kept in on purpose: this is where the honest answer
    # is "imperative Python is fine".
    def forward_join():
        out = set()
        for sr in needs.values():
            if sr.get("type") != "sysreq":
                continue
            for fid in sr.get("implements") or []:
                f = needs.get(fid)
                if not f or f.get("type") != "fsr":
                    continue
                hit = False
                for sid in f.get("derives_from") or []:
                    s = needs.get(sid)
                    if s and s.get("type") == "safety_goal" and s.get("asil") == "D":
                        out.add(sr["id"])
                        hit = True
                        break
                if hit:
                    break
        return out

    c_join, _ = measure(forward_join, repeat)
    wl["C_join"] = {"impls": {"forward_join": c_join}}

    # D. transitive closure — reverse BFS. The reverse index must be built by
    # hand (and per directive, in real builds); the visited-set is on the
    # author to remember.
    def closure():
        rev: dict[str, list[str]] = {}
        for nid, data in needs.items():
            for fld in sq.LINK_FIELDS:
                for tgt in data.get(fld) or []:
                    rev.setdefault(tgt, []).append(nid)
        seen: set[str] = set()
        stack = ["SG_0"]
        while stack:
            cur = stack.pop()
            for src in rev.get(cur, []):
                if src not in seen:
                    seen.add(src)
                    stack.append(src)
        return seen

    d_cl, _ = measure(closure, repeat)
    wl["D_closure"] = {"impls": {"hand_bfs": d_cl}}

    # E. the complex one: a forward join, two attribute predicates and an
    # anti-join in a single question. Same author-vs-expert pair as B, but now
    # the expert has to maintain TWO indexes and get their order right.
    def complex_author():
        out = set()
        for sr in needs.values():
            if sr.get("type") != "swreq" or sr.get("status") != "open":
                continue
            if not any(
                (needs.get(rid) or {}).get("type") == "req"
                and (needs.get(rid) or {}).get("status") == "open"
                for rid in sr.get("links") or []
            ):
                continue
            covered = False
            for other in needs.values():  # the inner scan again
                if other.get("type") == "impl" and sr["id"] in (other.get("implements") or []):
                    covered = True
                    break
            if not covered:
                out.add(sr["id"])
        return out

    def complex_expert():
        covered = set()
        for other in needs.values():
            if other.get("type") == "impl":
                covered.update(other.get("implements") or [])
        out = set()
        for sr in needs.values():
            if sr.get("type") != "swreq" or sr.get("status") != "open":
                continue
            if sr["id"] in covered:
                continue
            for rid in sr.get("links") or []:
                r = needs.get(rid)
                if r and r.get("type") == "req" and r.get("status") == "open":
                    out.add(sr["id"])
                    break
        return out

    e_scan, r_e_scan = measure(complex_author, repeat)
    e_idx, r_e_idx = measure(complex_expert, repeat)
    assert r_e_scan == r_e_idx
    wl["E_complex"] = {"impls": {"complex_author": e_scan, "complex_expert": e_idx}}

    return {"variant": "python", "compiled": False, "load_ms": round(load_ms, 3), "workloads": wl}


# -- the Cypher engines ---------------------------------------------------


def run_engine(graph_path: str, repeat: int, variant: str) -> dict:
    from needquery import PropertyGraph, ReferenceBackend
    import needquery.cypher.optimized as optmod

    t0 = time.perf_counter()
    graph = PropertyGraph.from_needs_json(graph_path)  # index build, once
    load_ms = (time.perf_counter() - t0) * 1000.0

    if variant == "optimized":
        be = ReferenceBackend.optimized(graph)
    elif variant == "bitmap":
        be = ReferenceBackend.bitmap(graph)
    else:
        be = ReferenceBackend(graph)
    compiled = variant == "optimized" and optmod.__file__.endswith(".so")
    impl = ("optimized_cython" if compiled else variant)

    wl: dict = {}
    for key, (cypher, var) in CYPHER.items():
        # parse time is included on purpose — it is paid per directive in real use
        stats, _ = measure(lambda c=cypher, v=var: set(be.select_ids(c, var=v)), repeat)
        wl[key] = {"impls": {impl: stats}}
    return {"variant": variant, "compiled": compiled, "load_ms": round(load_ms, 3), "workloads": wl}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--variant", required=True,
                    choices=["python", "reference", "optimized", "bitmap"])
    ap.add_argument("--repeat", type=int, default=5)
    args = ap.parse_args()

    if args.variant == "python":
        out = run_python(args.graph, args.repeat)
    else:
        out = run_engine(args.graph, args.repeat, args.variant)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
