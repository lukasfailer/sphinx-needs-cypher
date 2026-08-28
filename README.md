# needquery — declarative graph queries for Sphinx-Needs

A [Sphinx-Needs](https://github.com/useblocks/sphinx-needs) project *is* a
property graph: every need is a node, every typed link is an edge. But the
directives select nodes with imperative Python — a `filter_string` that is
`eval`'d per need, or a `filter_code` block that runs arbitrary code at build
time.

**needquery replaces that with a query language.** It is a pure-Python
reference engine for a subset of [openCypher](https://opencypher.org/), a
`.. needquery::` Sphinx directive that uses it, and a benchmark harness that
measures it honestly against hand-written Python, a commercial Rust engine and
Neo4j.

```cypher
MATCH (r:swreq) WHERE NOT ( ()-->(r) ) RETURN r
```

That query — "software requirements nothing traces to" — has no correct
`filter_string` equivalent, because a `filter_string` sees one need at a time
and never the edge.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). With [direnv](https://direnv.net/),
`direnv allow` does all of this on `cd`.

```bash
git clone https://github.com/lukasfailer/sphinx-needs-cypher
cd sphinx-needs-cypher
uv sync --all-extras

uv run pytest                # 56 tests, both executors
./scripts/run_all.sh         # every claim in this README, in order (~3 min)
```

### Or piece by piece

| Command | What you see |
| --- | --- |
| `uv run pytest` | Engine + shim tests; every engine test runs against **all three** executors |
| `uv run python examples/compare.py` | Each selection as Python-today vs Cypher on the real 292-need demo graph, with the failure modes live |
| `uv run python examples/security_rce_poc.py` | `filter_string` exfiltrates an env var at build time; the Cypher parser rejects code by grammar |
| `uv run python -m needquery.shim` | Migration shim: translates the mechanical `filter_string` subset, refuses the rest **by name** |
| `uv run python scripts/parity_check.py data/needs.ubc.json` | Reference engine vs the commercial `ubc` Rust engine: identical rows on every query |
| `./scripts/bench.sh` | The benchmark — `quick` preset, `--cython`, `--plot`, `incremental`, `llm`; see the script header |
| `./scripts/bench.sh --sizes 1000 --repeat 3` | A fast pass at any size you like |
| `./scripts/build_cython.sh` | Compiles the planned executor with Cython (needs a C compiler) |
| `uv run python bench/benchmark.py --with-neo4j` | Adds a Neo4j 5 lane in a throwaway docker container |

The presentation in `slides/` builds with `bun install && bun run dev`.

## The engine

| Path | What it is |
| --- | --- |
| `src/needquery/graph.py` | Indexed property graph — node-by-id, label buckets, typed forward/reverse adjacency, all built once at load |
| `src/needquery/cypher/parser.py` | Hand-written tokenizer + recursive-descent parser. No parser generator, no dependency |
| `src/needquery/cypher/executor.py` | The naive reference executor — walks the AST per row. Correct, simple, slow, and the honest baseline |
| `src/needquery/cypher/optimized.py` | The planned executor: predicate pushdown, semi-join decorrelation, compiled WHERE closures. Optionally Cython-compiled |
| `src/needquery/cypher/bitmap.py` | A third fork: attribute bitmaps, so a filter becomes set arithmetic |
| `src/needquery/incremental.py` | Index persistence across builds |
| `src/needquery/sphinx_ext.py` | The `.. needquery::` directive |
| `src/needquery/shim.py` | `filter_string` → Cypher migration, refusing what it cannot translate |
| `src/needquery/backends/` | Interchangeable backends: the reference engine, and a shim to `ubc query cypher` |

## Benchmark — how far can Python actually be driven?

`bench/benchmark.py` measures **9 lanes** across **5 workloads** at
**300 / 1 000 / 10 000 / 40 000 needs**, each variant in its own subprocess,
with all raw runs and environment metadata committed under `bench/results/`.

Every lane must return the identical row count per workload per size or the run
aborts — verified across all 172 measured rows in the committed results.

| Lane | What the number includes |
| --- | --- |
| Python author / expert | in-process call, graph already loaded; back-link build reported as `load_ms` |
| Cypher naive / + planner / + Cython / + bitmap | in-process call incl. query parse; index build reported as `load_ms` |
| `ubc_cli` — commercial Rust engine | **end-to-end** process wall clock: start + license check + cached-index load + query + full JSON output |
| `ubc_mcp` — same engine, warm | per-query latency against a persistent `ubc serve mcp`; server caps payload at 200 rows |
| `neo4j` — Neo4j 5 in docker (opt-in) | warm bolt latency, all rows fetched; one plan-compile run discarded |

Headline, **40 000 needs** (AMD Ryzen 7 7800X3D 8-Core Processor, CPython 3.12.13, best of 5, ms):

| Workload | author Python | expert Python | naive Cypher | + planner | + Cython | + bitmap | ubc e2e / warm | Neo4j |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| flat attribute filter | 211.8 | 1.7 | 25.7 | 4.3 | 3.4 | 1.6 | 130.4 / 68.6 | 49.8 |
| anti-join ("unverified") | 12,134 | 3.1 | 65.5 | 4.9 | 4.0 | 0.32 | 130.5 / 68.9 | 9.7 |
| forward 2-hop join | 1.8 | — | 9.5 | 2.7 | 2.6 | 2.6 | 129.0 / 72.6 | 8.3 |
| transitive closure | 54.3 | — | 13.6 | 0.03 | 0.03 | 0.03 | 132.5 / 72.9 | 1.0 |
| complex multi-clause | 5,577 | 4.9 | 62.8 | 13.9 | 11.8 | 13.9 | 137.3 / 81.5 | 21.8 |

The `ubc` and Neo4j columns are *different kinds of numbers* — see the lane
table. They are shown together because that is how each is actually consumed.

At **1 000 needs everything is under 9 ms in every lane**, `ubc` end-to-end
included. At small scale the whole debate is moot, which is itself a finding.

### What the tiers show

1. **The win against code people actually write is enormous.** On the anti-join
   the author's nested scan takes 12,134 ms and the planner takes 4.9 ms —
   **2,484×**. On the complex multi-clause workload it is 5,577 ms vs
   13.9 ms. Authors scan; engines index.
2. **The gap to an expert is a missing planner, not a slow language.** The naive
   interpreter is 21× behind the hand-indexed expert (65.5 ms vs
   3.1 ms). Three classic rewrites — pushdown, decorrelation, compiled WHERE
   — close that to 1.6×. Declarative queries make those rewrites
   *possible*; `filter_code` hides the intent, so no tool can ever apply them.
3. **Cython is not the lever** — roughly 1.2× on top of the planner. The hot work
   already runs in C (dicts, sets), so compiling the interpreter only shaves
   constants. Changing the *data layout* is the real lever: the bitmap executor
   does the same anti-join in 0.32 ms, **10× faster than the
   hand-indexed expert**.
4. **Where imperative Python is genuinely fine, the numbers say so.** On the
   forward 2-hop join, hand-written Python (1.8 ms) beats every Cypher tier.
   No overclaiming.

### Easier for an LLM — measured, with a caveat

`bench/llm_eval.py` gives claude-sonnet-5 12 natural-language selection
tasks and asks for both a `filter_string` and a Cypher query, then runs both:

- `filter_string`: **9 correct**, and
  **3 of 12 could not be expressed at all**
- Cypher: **10 correct** of 12

So on the tasks Python *can* express, the model did fine. The real difference
is reach, not fluency — a quarter of the tasks have no Python form to get right.

## Parity — the reference engine and the commercial engine agree

`scripts/parity_check.py` runs every query through the pure-Python engine and
the `ubc` Rust engine (v0.32.0) on the identical 292-need graph:

```
[untraced_swreqs]      ref=  2  ubc=  2  OK
[asil_d_safety_goals]  ref= 20  ubc= 20  OK
[sysreq_to_asil_d]     ref= 18  ubc= 18  OK
[unverified_swreqs]    ref=  8  ubc=  8  OK
[hazard_trace_closure] ref= 74  ubc= 74  OK
```

Same language, same answers, different speed and scale. The unit suite
additionally runs every engine test against the naive, optimized **and** bitmap
executors, so the optimizations cannot drift from the semantics.

## Security

`filter_string` and `filter_code` execute document-controlled code at build
time — typically in CI, next to the secrets. Sphinx-Needs' own documentation
says to be sure you trust the input and the writers.
`examples/security_rce_poc.py` demonstrates that documented behaviour (it reads
a fake env var it sets itself; it is not a 0-day). A query language is parsed,
not executed: there is **no grammar production in this parser that reaches a
function call, an import, or a syscall**.

## Scope & honesty notes

- The engine implements a **subset** of openCypher — labelled nodes,
  directed/variable-length relationships, `WHERE` with comparisons, boolean and
  pattern predicates, `RETURN`/`ORDER BY`/`LIMIT`. The boundary is deliberate
  and the parser enforces it rather than silently accepting more.
- `data/needs.ubc.json` is built from useblocks'
  [`sphinx-needs-demo`](https://github.com/useblocks/sphinx-needs-demo), so both
  engines index the identical graph. Source paths in that file are normalised to
  repo-relative form.
- Benchmark graphs are deterministic from a seed; `bench/results/` carries the
  raw runs, machine, interpreter and timestamp behind every number above.
- The `ubc` lanes need the commercial binary on `PATH`; they are skipped, not
  faked, when it is absent. The Neo4j lane is opt-in and needs docker.
- `bench/results/incremental.json` includes cases where incremental loading is
  **slower** than a full rebuild. Those are left in.

## Known limitations

See `.github/workflows/ci.yml` for the pipeline (manual-dispatch only — this
project has no Actions runners budgeted). Open issues in the engine itself:
non-ASCII string literals in queries are mangled by the tokenizer, `ORDER BY`
is ignored unless the key is also in `RETURN`, and `*0..n` parses but never
matches the zero-length path.

## License

MIT — see [`LICENSE`](LICENSE). The demo project it builds on is MIT too.
