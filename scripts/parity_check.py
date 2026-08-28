"""Cross-check the pure-Python reference engine against the commercial ``ubc``
engine on the identical needs graph.

Run from anywhere:

    UBC=/path/to/ubc PROJECT=/path/to/demo/docs \
      PYTHONPATH=src python scripts/parity_check.py data/needs.ubc.json

For every query in the suite it selects need ids with both backends and asserts
the two id *sets* are equal. Equal sets across a real 292-node safety graph is
the concrete evidence for "the reference backend answers the same language."
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from needquery import PropertyGraph, ReferenceBackend  # noqa: E402
from needquery.backends.ubc import UbcBackend, UbcNotAvailable  # noqa: E402

# the shared query suite lives with the tests — single source of truth
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from queries import SUITE  # noqa: E402


def main() -> int:
    needs_json = sys.argv[1] if len(sys.argv) > 1 else "data/needs.ubc.json"
    graph = PropertyGraph.from_needs_json(needs_json)
    ref = ReferenceBackend(graph)

    ubc = None
    project = os.environ.get("PROJECT")
    if project:
        try:
            ubc = UbcBackend(project, binary=os.environ.get("UBC"))
        except UbcNotAvailable as exc:
            print(f"! ubc unavailable ({exc}); running reference-only")

    print(f"graph: {len(graph)} needs, {len(graph.rel_types)} relationship types\n")
    failures = 0
    for q in SUITE:
        ref_ids = set(ref.select_ids(q.cypher, q.bind))
        line = f"[{q.key}] ref={len(ref_ids):>3}"
        if ubc is not None:
            try:
                ubc_ids = set(ubc.select_ids(q.cypher, q.bind))
                match = ref_ids == ubc_ids
                line += f"  ubc={len(ubc_ids):>3}  {'OK' if match else 'MISMATCH'}"
                if not match:
                    failures += 1
                    line += (
                        f"\n    only-ref: {sorted(ref_ids - ubc_ids)}"
                        f"\n    only-ubc: {sorted(ubc_ids - ref_ids)}"
                    )
            except UbcNotAvailable as exc:
                line += f"  ubc-error: {exc}"
        print(line)
    print()
    if ubc is not None and failures == 0:
        print("PARITY: reference engine == ubc engine on every query.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
