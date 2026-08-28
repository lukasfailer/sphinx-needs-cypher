# Examples

- **`compare.py`** — Part A, runnable. For each of the five realistic selections
  it prints the Cypher answer next to the Python-today answer and demonstrates
  the exact failure mode (`PYTHONPATH=src python examples/compare.py`).
- **`status_quo.py`** — a faithful, minimal re-implementation of the Sphinx-Needs
  `filter_string` (`eval` per need) and `filter_code` mechanisms, plus the
  reverse-link computation, so the comparison is fair.
- **`rst_demo/`** — a minimal Sphinx project that **builds**, rendering a native
  `needtable` (Python filter) next to a `needquery` directive (Cypher) selecting
  the same set:

  ```bash
  PYTHONPATH=../../src python -m sphinx -b html rst_demo /tmp/out
  # /tmp/out/index.html shows the Cypher directive selecting SWREQ_C
  ```
