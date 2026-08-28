"""The benchmark engine: measure every selection strategy at every scale, from
one command, reproducibly.

    .venv/bin/python bench/benchmark.py                 # default sizes
    .venv/bin/python bench/benchmark.py --sizes 1000 40000 --repeat 7

What it does:

* Generates deterministic synthetic graphs (seeded — identical on every
  machine) at the requested TOTAL need counts into ``bench/graphs/``.
* Runs each engine variant in its own subprocess (cold interpreter, no
  cross-contamination):
    - ``python``            filter_string eval / AST-fast-path / filter_code
                            author-scan / expert-index / hand BFS
    - ``reference``         the naive pure-Python Cypher interpreter
    - ``optimized``         the planned executor (pushdown, decorrelation,
                            compiled WHERE)
    - ``optimized+cython``  the same file compiled by Cython
                            (run ``scripts/build_cython.sh`` first; skipped with a
                            notice when the build is absent)
    - ``ubc``               the commercial Rust engine, measured end-to-end as a
                            CLI and warm over its MCP server (see
                            ``bench/ubc_variant.py``; auto-enabled when the
                            binary is found via $UBC, PATH, or ``.tools/ubc``,
                            skipped with a notice otherwise)
    - ``neo4j``             a real Neo4j 5 server in docker, warm bolt queries
                            (opt-in via ``--with-neo4j``; see
                            ``bench/neo4j_variant.py``)
* Cross-checks that every implementation returns the same row count per
  workload, per size — a benchmark that returns different answers is measuring
  bugs, not speed.
* Writes ``bench/results/results-<UTC timestamp>.json`` (all raw runs +
  environment metadata) and the matching ``.csv``, then prints the summary
  table. Each run lands in its own timestamped pair, so no run ever overwrites
  another — or the committed ``results.json`` baseline the README quotes.

Why these sizes (receipts are the linked sphinx-needs issues):
    292     the real sphinx-needs-demo project (correctness work in this repo)
    1 000   a typical mid-size project
    10 000  the scale ubmarco benchmarked filters at in issue #328
    40 000  the ~50k-need automotive project in issue #1219 (2-5 h builds)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SIZES = [300, 1000, 10000, 40000]

# Workload -> (the naive/author impl, the expert impl or None)
BASELINES = {
    "A_flat": ("filter_string_eval", "ast_fastpath"),
    "B_antijoin": ("author_scan", "expert_index"),
    "C_join": ("forward_join", None),
    "D_closure": ("hand_bfs", None),
    "E_complex": ("complex_author", "complex_expert"),
}
ENGINE_IMPLS = ["reference", "optimized", "optimized_cython", "bitmap"]
EXTRA_IMPLS = ["ubc_cli", "ubc_mcp", "neo4j"]  # measured lanes beyond the core plans

NEO4J_CONTAINER = "needquery-bench-neo4j"
NEO4J_BOLT_PORT = 17687  # non-default, avoids colliding with a local Neo4j


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def gen_graph(total: int) -> Path:
    sys.path.insert(0, str(ROOT / "bench"))
    import generate

    out_dir = ROOT / "bench" / "graphs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"graph_{total}.json"
    n_swreq = generate.n_swreq_for_total(total)
    needs = generate.write(path, n_swreq)
    print(f"  graph_{total}.json: {needs} needs (n_swreq={n_swreq}, seed=1)")
    return path


def run_variant(graph: Path, variant: str, repeat: int, pythonpath: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    cmd = [
        sys.executable,
        str(ROOT / "bench" / "_run_variant.py"),
        "--graph", str(graph),
        "--variant", variant,
        "--repeat", str(repeat),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"{variant} failed:\n{out.stderr}")
    return json.loads(out.stdout)


def run_script(script: str, graph: Path, repeat: int, extra: list[str] | None = None) -> dict:
    cmd = [sys.executable, str(ROOT / "bench" / script),
           "--graph", str(graph), "--repeat", str(repeat), *(extra or [])]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"{script} failed:\n{out.stderr}")
    return json.loads(out.stdout)


def find_ubc() -> str | None:
    sys.path.insert(0, str(ROOT / "bench"))
    import ubc_variant

    return ubc_variant.find_ubc()


def start_neo4j() -> None:
    subprocess.run(["docker", "rm", "-f", NEO4J_CONTAINER],
                   capture_output=True, text=True)
    out = subprocess.run(
        ["docker", "run", "-d", "--name", NEO4J_CONTAINER,
         "-e", "NEO4J_AUTH=none",
         "-p", f"{NEO4J_BOLT_PORT}:7687",
         "neo4j:5"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"could not start Neo4j container:\n{out.stderr}")
    print(f"started Neo4j container {NEO4J_CONTAINER} (bolt :{NEO4J_BOLT_PORT}); "
          "the variant waits for it to accept connections")


def stop_neo4j() -> None:
    subprocess.run(["docker", "rm", "-f", NEO4J_CONTAINER],
                   capture_output=True, text=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES,
                    help="total need counts (default: %(default)s)")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "bench" / "results"))
    ap.add_argument("--with-neo4j", action="store_true",
                    help="also measure Neo4j 5 in a throwaway docker container "
                         "(bench/neo4j_variant.py; needs the neo4j driver and docker)")
    args = ap.parse_args()

    cython_pkg = ROOT / "build" / "cython" / "needquery" / "cypher" / "optimized.so"
    have_cython = cython_pkg.exists()
    if not have_cython:
        print("note: no Cython build found — run ./scripts/build_cython.sh to add the "
              "'optimized+cython' tier. Continuing without it.\n")
    else:
        # A build/cython left behind by a different interpreter (e.g. built with
        # the .venv's 3.12, run under a system 3.14) fails at import time with an
        # undefined-symbol error. Probe it in a subprocess so a stale build skips
        # the tier instead of aborting the whole benchmark.
        probe = subprocess.run(
            [sys.executable, "-c", "import needquery.cypher.optimized"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "build" / "cython")},
            cwd=ROOT)
        if probe.returncode != 0:
            print("note: build/cython exists but does not import under "
                  f"{sys.executable} (stale build from another Python?) — "
                  "skipping the 'optimized+cython' tier. Rebuild: "
                  "PYTHON=$PY ./scripts/build_cython.sh\n")
            have_cython = False

    plans = [
        ("python", str(ROOT / "examples")),
        ("reference", str(ROOT / "src")),
        ("optimized", str(ROOT / "src")),
    ]
    if have_cython:
        plans.append(("optimized", str(ROOT / "build" / "cython")))
    plans.append(("bitmap", str(ROOT / "src")))

    ubc = find_ubc()
    if not ubc:
        print("note: `ubc` binary not found ($UBC, PATH, or .tools/ubc) — skipping the "
              "commercial-engine lane. To add it:\n"
              "  curl -fL -o .tools/ubc https://download.useblocks.com/ubc/0.33.0/ubc-linux-x64-0.33.0\n"
              "  chmod +x .tools/ubc\n"
              "  git clone --depth 1 https://github.com/useblocks/sphinx-needs-demo .tools/sphinx-needs-demo\n"
              "(the clone hosts the bench project — ubc's free OSS license is granted "
              "per publicly-reachable git repo)\n")

    if args.with_neo4j:
        start_neo4j()

    def collect(res: dict, total: int, needs_total: int, counts: dict[str, set]) -> None:
        for wl, data in res["workloads"].items():
            for impl, stats in data["impls"].items():
                rows.append({
                    "size": total,
                    "needs": needs_total,
                    "workload": wl,
                    "impl": impl,
                    "ms_best": stats["best"],
                    "ms_median": stats["median"],
                    "runs": stats["runs"],
                    "count": stats["count"],
                    "load_ms": res["load_ms"],
                })
                counts.setdefault(wl, set()).add(stats["count"])

    rows: list[dict] = []
    try:
        for total in args.sizes:
            print(f"size {total}:")
            graph = gen_graph(total)
            needs_total = json.loads(graph.read_text())["versions"]["1.0"]["needs_amount"]
            counts: dict[str, set] = {}
            for variant, pp in plans:
                res = run_variant(graph, variant, args.repeat, pp)
                collect(res, total, needs_total, counts)
                label = "optimized+cython" if res.get("compiled") else variant
                print(f"  {label:18} done (load {res['load_ms']:.1f} ms)")
            if ubc:
                res = run_script("ubc_variant.py", graph, args.repeat)
                collect(res, total, needs_total, counts)
                print(f"  {'ubc':18} done (first-run {res['load_ms']:.1f} ms, "
                      f"no-cache {res['no_cache_ms']:.1f} ms)")
            if args.with_neo4j:
                res = run_script("neo4j_variant.py", graph, args.repeat,
                                 ["--uri", f"bolt://localhost:{NEO4J_BOLT_PORT}"])
                collect(res, total, needs_total, counts)
                print(f"  {'neo4j':18} done (cold load {res['load_ms']:.1f} ms)")
            for wl, cs in counts.items():
                if len(cs) != 1:
                    raise SystemExit(f"COUNT MISMATCH at size {total}, {wl}: {cs}")
            print(f"  counts agree across all implementations: "
                  f"{ {wl: cs.pop() for wl, cs in counts.items()} }")
    finally:
        if args.with_neo4j:
            stop_neo4j()

    run_at = datetime.now(timezone.utc)
    stamp = run_at.strftime("%Y%m%dT%H%M%SZ")

    meta = {
        "timestamp": run_at.isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "cpu": cpu_model(),
        "platform": platform.platform(),
        "repeat": args.repeat,
        "timing": "best of N (all raw runs kept in 'runs'); parse time included; "
                  "graph load/index build reported separately as load_ms",
        "ubc_tier": bool(ubc),
        "ubc_timing": "ubc_cli = end-to-end CLI wall clock (process start + license "
                      "check + cached-index load + query + full JSON output); "
                      "ubc_mcp = warm per-query latency against a persistent "
                      "`ubc serve mcp` (stdio JSON-RPC round-trip included, result "
                      "payload capped at 200 rows by the server); first MCP call "
                      "discarded; load_ms = first CLI run incl. index build",
        "neo4j_tier": args.with_neo4j,
        "neo4j_timing": "warm bolt query latency against a running neo4j:5 docker "
                        "container, all rows fetched client-side; one untimed "
                        "plan-compile run discarded; load_ms = cold JSON->Neo4j "
                        "load incl. :Need(id) index; server start not included",
        "cython_tier": have_cython,
        "seed": 1,
        "command": " ".join(sys.argv),
    }

    # Every run gets its own timestamped pair, so a quick smoke run can never
    # overwrite the blessed results the README quotes. Promote a run to the
    # baseline deliberately:
    #     cp bench/results/results-<stamp>.json bench/results/results.json
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"results-{stamp}.json"
    csv_path = out_dir / f"results-{stamp}.csv"
    json_path.write_text(
        json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "size", "needs", "workload", "impl", "ms_best", "ms_median", "count", "load_ms"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    print(f"\nwrote {json_path.name} and {csv_path.name} in {out_dir}")
    print(f"      (the committed baseline results.json/.csv is left untouched; "
          f"copy over it to promote this run)")

    print_table(rows)


def print_table(rows: list[dict]) -> None:
    by = {(r["size"], r["workload"], r["impl"]): r for r in rows}
    sizes = sorted({r["size"] for r in rows})
    extra = [i for i in EXTRA_IMPLS if any(r["impl"] == i for r in rows)]
    for size in sizes:
        needs = next(r["needs"] for r in rows if r["size"] == size)
        print(f"\n=== {needs} needs (size {size}) — ms, best of N ===")
        impls_order = []
        for wl, (naive, expert) in BASELINES.items():
            impls_order = [naive] + ([expert] if expert else []) + ENGINE_IMPLS + extra
            cells = []
            for impl in impls_order:
                r = by.get((size, wl, impl))
                cells.append(f"{r['ms_best']:>10.2f}" if r else f"{'—':>10}")
            print(f"{wl:12}" + "".join(f"{n[:10]:>11}" for n in impls_order))
            print(f"{'':12}" + "".join(cells))


if __name__ == "__main__":
    main()
