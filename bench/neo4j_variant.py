"""Measure Neo4j (community server, official bolt driver) on the same graphs.

Opt-in: ``bench/benchmark.py --with-neo4j`` starts a throwaway docker container
(``neo4j:5``, auth disabled, bolt on a non-default port) and runs this script
per size. Requires the ``neo4j`` Python driver (``pip install .[bench]``).

What is measured, exactly:

* ``load_ms`` — COLD load: wipe the database, then push the needs JSON into
  Neo4j over bolt (batched ``UNWIND … CREATE`` per label / relationship type),
  create + await an index on ``:Need(id)``. Reported separately, like the
  index-build ``load_ms`` of the in-process engines.
* ``neo4j`` impl rows — WARM per-query latency: the workload Cypher sent over
  bolt to the already-loaded server, all result rows fetched by the client.
  Per workload one untimed warm-up run (plan compilation) is discarded, then
  best-of-N — the same discipline as the other lanes. Server process start is
  NOT included; client-server round-trip and result transfer ARE.

The graph is loaded through :class:`needquery.PropertyGraph` (the same loader
every other lane uses), so nodes, labels and typed relationships are identical
by construction. Workload queries are the same Cypher texts as
``_run_variant.CYPHER`` with the projection narrowed to ``RETURN <var>.id`` —
the id list is what ``select_ids`` returns elsewhere. Row counts are DISTINCT
ids and feed the benchmark's counts-agree cross-check.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needquery import PropertyGraph  # noqa: E402

MAX_REPEAT_MS = 3000.0
BATCH = 5000

CYPHER_IDS = {
    "A_flat": "MATCH (r:swreq) WHERE r.status = 'open' RETURN r.id",
    "B_antijoin": "MATCH (r:swreq) WHERE NOT ( (r)<-[:links]-(:test) ) RETURN r.id",
    "C_join": (
        "MATCH (sr:sysreq)-[:implements]->(f:fsr)-[:derives_from]->(s:safety_goal) "
        "WHERE s.asil = 'D' RETURN sr.id"
    ),
    "D_closure": "MATCH (h:safety_goal)<-[*1..]-(n) WHERE h.id = 'SG_0' RETURN n.id",
    "E_complex": "MATCH (sr:swreq)-[:links]->(r:req) WHERE sr.status = 'open' AND r.status = 'open' AND NOT ( (sr)<-[:implements]-(:impl) ) RETURN sr.id",
}


def connect(uri: str):
    from neo4j import GraphDatabase

    last: Exception | None = None
    for _ in range(60):  # the container may still be starting
        try:
            driver = GraphDatabase.driver(uri, auth=None)
            driver.verify_connectivity()
            return driver
        except Exception as exc:  # noqa: BLE001 - driver raises many types here
            last = exc
            time.sleep(1.0)
    raise SystemExit(f"cannot reach Neo4j at {uri}: {last}")


def scalar_props(attrs) -> dict:
    return {k: v for k, v in attrs.items() if isinstance(v, (str, int, float, bool))}


def load(driver, graph: PropertyGraph) -> float:
    """Wipe + load + index. Returns wall-clock ms (the cold load number)."""
    t0 = time.perf_counter()
    with driver.session() as s:
        # wipe (batched; implicit transaction required for IN TRANSACTIONS)
        s.run(
            "MATCH (n) CALL { WITH n DETACH DELETE n } "
            "IN TRANSACTIONS OF 10000 ROWS"
        ).consume()
        s.run("CREATE INDEX need_id IF NOT EXISTS FOR (n:Need) ON (n.id)").consume()

        by_label: dict[str, list[dict]] = {}
        for node in graph.nodes():
            by_label.setdefault(node.label, []).append(scalar_props(node.attrs))
        for label, rows in by_label.items():
            q = f"UNWIND $rows AS p CREATE (n:Need:`{label}`) SET n = p"
            for i in range(0, len(rows), BATCH):
                s.run(q, rows=rows[i:i + BATCH]).consume()
        s.run("CALL db.awaitIndexes()").consume()

        by_rel: dict[str, list[list[str]]] = {}
        for node in graph.nodes():
            for rel in graph.rel_types:
                for dst in graph.out(node.id, [rel]):
                    by_rel.setdefault(rel, []).append([node.id, dst])
        for rel, pairs in by_rel.items():
            q = (
                "UNWIND $pairs AS p "
                "MATCH (a:Need {id: p[0]}), (b:Need {id: p[1]}) "
                f"CREATE (a)-[:`{rel}`]->(b)"
            )
            for i in range(0, len(pairs), BATCH):
                s.run(q, pairs=pairs[i:i + BATCH]).consume()
    return (time.perf_counter() - t0) * 1000.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--uri", default="bolt://localhost:17687")
    args = ap.parse_args()

    graph = PropertyGraph.from_needs_json(args.graph)
    driver = connect(args.uri)
    try:
        load_ms = load(driver, graph)

        wl: dict = {}
        with driver.session() as s:
            for key, cypher in CYPHER_IDS.items():
                s.run(cypher).value()  # warm-up: plan compile, page cache; discarded
                runs: list[float] = []
                ids: set = set()
                for _ in range(args.repeat):
                    t0 = time.perf_counter()
                    ids = set(s.run(cypher).value())
                    runs.append((time.perf_counter() - t0) * 1000.0)
                    if runs[0] > MAX_REPEAT_MS:
                        break
                wl[key] = {"impls": {"neo4j": {
                    "runs": [round(r, 3) for r in runs],
                    "best": round(min(runs), 3),
                    "median": round(median(runs), 3),
                    "count": len(ids),
                }}}
    finally:
        driver.close()

    json.dump({"variant": "neo4j", "compiled": False,
               "load_ms": round(load_ms, 3), "workloads": wl}, sys.stdout)


if __name__ == "__main__":
    main()
