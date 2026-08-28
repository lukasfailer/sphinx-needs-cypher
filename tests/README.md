# Tests

- **`test_engine.py`** — unit tests for the parser, executor and graph model on a
  tiny hand-built graph (label match, directed hop, variable length, cycle
  termination, pattern predicate, anti-join).
- **`test_shim.py`** — the `filter_string → Cypher` shim must be *result-
  equivalent*: the emitted Cypher selects exactly what the original filter did.
- **`parity_check.py`** — reference engine vs the `ubc` Rust engine on identical
  data; identical rows on every query. Run:

  ```bash
  PROJECT=/path/to/sphinx-needs-demo/docs UBC=/path/to/ubc \
    PYTHONPATH=src python scripts/parity_check.py data/needs.ubc.json
  ```
- **`queries.py`** — the single shared query suite that drives parity, examples
  and the benchmark, so a demonstrated query is also the tested one.
