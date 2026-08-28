"""Run ONE selection against a graph and print the matches — not a benchmark.

    # the fastest engine (bitmap) on a 400 000-need graph
    scripts/query.py --size 400000 "MATCH (r:swreq) WHERE r.status = 'open' RETURN r"

    # the same selection the way open-source Sphinx-Needs does it today
    scripts/query.py --size 400000 --engine python \
        --filter "type == 'swreq' and status == 'open'"

The graph is generated once into ``bench/graphs/graph_<size>.json`` and reused
on later runs, so only the first call at a given size pays for generation.

Load time and query time are reported separately: the engines build their
indexes at load, which is the whole point — that cost is paid once per build,
the query cost is paid per directive.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "examples"))

ENGINES = ("bitmap", "optimized", "reference", "python")


def graph_path(size: int) -> Path:
    """Generate the graph for this size once, then reuse it."""
    import generate

    out_dir = ROOT / "bench" / "graphs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"graph_{size}.json"
    if not path.exists():
        n_swreq = generate.n_swreq_for_total(size)
        print(f"generating {path.name} (n_swreq={n_swreq}, seed=1) …", flush=True)
        total = generate.write(path, n_swreq)
        print(f"  {total} needs, {path.stat().st_size / 1e6:.1f} MB")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cypher", nargs="?", help="the openCypher query to run")
    ap.add_argument("--engine", choices=ENGINES, default="bitmap",
                    help="bitmap is the fastest; python is the Sphinx-Needs "
                         "status quo and needs --filter instead of a query "
                         "(default: %(default)s)")
    ap.add_argument("--size", type=int, default=40000,
                    help="total needs in the graph (default: %(default)s)")
    ap.add_argument("--graph", help="use this needs.json instead of generating one")
    ap.add_argument("--filter", dest="filter_string",
                    help="a Sphinx-Needs filter_string, for --engine python")
    ap.add_argument("--show", type=int, default=10,
                    help="how many matching ids to print (default: %(default)s)")
    args = ap.parse_args()

    if args.engine == "python":
        if not args.filter_string:
            ap.error("--engine python needs --filter (it cannot run Cypher)")
    elif not args.cypher:
        ap.error("give a Cypher query, or use --engine python with --filter")

    path = Path(args.graph) if args.graph else graph_path(args.size)

    t0 = time.perf_counter()
    if args.engine == "python":
        import status_quo as sq

        needs = sq.load_needs(path)  # includes the back-link build
        load_ms = (time.perf_counter() - t0) * 1000.0
        t1 = time.perf_counter()
        ids = sq.filter_string(needs, args.filter_string)
        query_ms = (time.perf_counter() - t1) * 1000.0
        shown = f"filter_string: {args.filter_string}"
    else:
        from needquery.graph import PropertyGraph
        from needquery.cypher.parser import parse

        graph = PropertyGraph.from_needs_json(path)
        if args.engine == "bitmap":
            from needquery.cypher.bitmap import BitmapExecutor as Executor
        elif args.engine == "optimized":
            from needquery.cypher.optimized import OptimizedExecutor as Executor
        else:
            from needquery.cypher.executor import Executor
        ex = Executor(graph)
        load_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        rows = ex.run(parse(args.cypher))
        query_ms = (time.perf_counter() - t1) * 1000.0
        ids = [next(iter(r.values())) for r in rows]
        shown = f"cypher: {args.cypher}"

    print(f"\ngraph   {path.name}")
    print(f"engine  {args.engine}")
    print(f"{shown}")
    print(f"\nload    {load_ms:9.1f} ms   (parse + index build, once per build)")
    print(f"query   {query_ms:9.2f} ms   (per directive)")
    print(f"matched {len(ids)} needs")
    for nid in ids[: args.show]:
        print(f"  {nid}")
    if len(ids) > args.show:
        print(f"  … {len(ids) - args.show} more (raise --show to see them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
