"""Do the Part A claims hold for genuinely COMPLEX queries?

The published benchmark now carries E_complex as a fifth workload (a join, two
attribute predicates and an anti-join in one query) across all nine lanes. This
script goes one step further and probes two shapes the main benchmark does NOT
run, to check that the planner's advantage is not an artefact of query shape.

Three queries, each strictly harder than any published lane:

    E1  a join, two attribute predicates AND an anti-join in one query
    E2  a three-node / two-edge join with predicates on two of the nodes
    E3  a bounded variable-length path with predicates on both ends

All three run on all three executors; rows are compared across executors, and
E1 additionally against two hand-written Python versions (author and expert).

    python bench/complex_queries.py [n_swreq]      # default 10000 (~40k needs)
"""
from __future__ import annotations

import json, sys, time, statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "examples"))

from generate import make_graph            # noqa: E402
from needquery import PropertyGraph, ReferenceBackend  # noqa: E402

N_SWREQ = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
REPEAT = 5

QUERIES = {
    "E1_join+2filters+antijoin":
        ("MATCH (sr:swreq)-[:links]->(r:req) "
         "WHERE sr.status = 'closed' AND r.status = 'open' "
         "AND NOT ( (sr)<-[:links]-(:test) ) RETURN sr", "sr"),
    "E2_3hop+2filters":
        ("MATCH (sr:sysreq)-[:implements]->(f:fsr)-[:derives_from]->(s:safety_goal) "
         "WHERE s.asil = 'D' AND f.asil = 'D' AND sr.status = 'open' RETURN sr", "sr"),
    "E3_varlen<=3+filter":
        ("MATCH (s:safety_goal)<-[*1..3]-(n) "
         "WHERE s.asil = 'D' AND n.status = 'open' RETURN n", "n"),
}

path = ROOT / "bench" / "graphs" / f"complex-{N_SWREQ}.json"
path.parent.mkdir(parents=True, exist_ok=True)
if not path.exists():
    path.write_text(json.dumps(make_graph(N_SWREQ)))
needs_total = len(json.loads(path.read_text())["versions"]["1.0"]["needs"])
print(f"graph: {needs_total} needs\n")


def best(fn, repeat=REPEAT):
    runs, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        runs.append((time.perf_counter() - t0) * 1000)
        if runs[0] > 3000:
            break
    return min(runs), out


t0 = time.perf_counter()
graph = PropertyGraph.from_needs_json(str(path))
print(f"index build: {(time.perf_counter()-t0)*1000:.1f} ms\n")

backends = {
    "reference": ReferenceBackend(graph),
    "planner": ReferenceBackend.optimized(graph),
    "bitmap": ReferenceBackend.bitmap(graph),
}

# --- the Python baselines, hand-written for E1 ---------------------------
import status_quo as sq  # noqa: E402
needs = sq.load_needs(str(path))


def py_author():
    """What an author writes: nested scans, no index."""
    out = set()
    for sr in needs.values():
        if sr.get("type") != "swreq" or sr.get("status") != "closed":
            continue
        ok_req = False
        for rid in sr.get("links", []):
            r = needs.get(rid)
            if r and r.get("type") == "req" and r.get("status") == "open":
                ok_req = True
                break
        if not ok_req:
            continue
        tested = False
        for other in needs.values():                    # the inner scan
            if other.get("type") == "test" and sr["id"] in other.get("links", []):
                tested = True
                break
        if not tested:
            out.add(sr["id"])
    return out


def py_expert():
    """What an expert writes: one pass, two indexes."""
    tested = set()
    by_id = needs
    for n in needs.values():
        if n.get("type") == "test":
            tested.update(n.get("links", []))
    out = set()
    for sr in needs.values():
        if sr.get("type") != "swreq" or sr.get("status") != "closed":
            continue
        if sr["id"] in tested:
            continue
        for rid in sr.get("links", []):
            r = by_id.get(rid)
            if r and r.get("type") == "req" and r.get("status") == "open":
                out.add(sr["id"])
                break
    return out


rows = []
for key, (q, var) in QUERIES.items():
    line = {"query": key}
    ref_rows = None
    for name, be in backends.items():
        try:
            ms, res = best(lambda b=be, q=q, v=var: set(b.select_ids(q, var=v)))
            line[name] = f"{ms:.2f} ms"
            line[name + "_n"] = len(res)
            if ref_rows is None:
                ref_rows = res
            elif res != ref_rows:
                line[name] += "  ROW MISMATCH"
        except Exception as e:  # noqa: BLE001
            line[name] = f"unsupported: {type(e).__name__}: {e}"
    rows.append(line)

ms_a, r_a = best(py_author, repeat=3)
ms_e, r_e = best(py_expert)
print(f"E1 python author : {ms_a:10.2f} ms  ({len(r_a)} rows)")
print(f"E1 python expert : {ms_e:10.2f} ms  ({len(r_e)} rows)")
ms_c, r_c = best(lambda: set(backends['planner'].select_ids(*[QUERIES['E1_join+2filters+antijoin'][0]], var='sr')))
print(f"E1 cypher planner: {ms_c:10.2f} ms  ({len(r_c)} rows)  same rows as expert: {r_c == r_e}")
print()
for line in rows:
    print(line["query"])
    for name in backends:
        n = line.get(name + "_n")
        print(f"   {name:10} {line[name]:>28}   rows={n}")
    print()
