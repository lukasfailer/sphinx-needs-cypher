"""Generate a synthetic Sphinx-Needs graph with realistic traceability shape at
an arbitrary size, so the benchmark can vary N without hand-authoring needs.

The shape mirrors the demo project's safety hierarchy:

    safety_goal  <-derives_from-  fsr  <-implements-  sysreq
         ^                                               |
         | (asil on some safety_goals)                   |
    req  <-links-  swreq  <-implements-  impl            |
                     ^                                    |
                     |  <-links-  test                    |

Every need gets typed attributes (status, asil) and typed links. The generator
is deterministic given a seed passed in, so a benchmark run is reproducible and
the reference engine and the filter path see the identical graph.
"""

from __future__ import annotations

import json
from pathlib import Path


def make_graph(n_swreq: int, seed: int = 1) -> dict:
    """Build a project with ~n_swreq software requirements and the layers around
    them. Total need count is roughly 4x n_swreq."""
    rng = _Lcg(seed)
    needs: dict[str, dict] = {}

    def add(nid: str, ntype: str, **fields):
        needs[nid] = {"id": nid, "type": ntype, "title": nid, **fields}

    n_sg = max(4, n_swreq // 8)
    n_fsr = max(4, n_swreq // 4)
    n_sysreq = max(4, n_swreq // 4)
    n_req = max(4, n_swreq // 3)
    n_impl = n_swreq
    n_test = n_swreq

    for i in range(n_sg):
        add(f"SG_{i}", "safety_goal", asil="D" if i % 3 == 0 else "B",
            status="open")
    for i in range(n_fsr):
        add(f"FSR_{i}", "fsr", asil="D" if i % 2 == 0 else "C", status="open",
            derives_from=[f"SG_{rng.below(n_sg)}"])
    for i in range(n_sysreq):
        add(f"SYSREQ_{i}", "sysreq", asil="D", status="open",
            implements=[f"FSR_{rng.below(n_fsr)}"])
    for i in range(n_req):
        add(f"REQ_{i}", "req", status="open" if i % 2 else "closed")
    for i in range(n_swreq):
        add(f"SWREQ_{i}", "swreq",
            status="open" if i % 2 else "closed",
            links=[f"REQ_{rng.below(n_req)}"])
    for i in range(n_impl):
        add(f"IMPL_{i}", "impl", status="open",
            implements=[f"SWREQ_{rng.below(n_swreq)}"])
    # Leave ~5% of swreqs unverified by making tests skip them.
    for i in range(n_test):
        if i % 20 == 0:
            add(f"TEST_{i}", "test", status="open", links=[f"REQ_{rng.below(n_req)}"])
        else:
            add(f"TEST_{i}", "test", status="open", links=[f"SWREQ_{i % n_swreq}"])

    return {
        "current_version": "1.0",
        "project": f"synthetic-{n_swreq}",
        "versions": {
            "1.0": {
                "creator": {"program": "bench.generate", "version": "1"},
                "needs_amount": len(needs),
                "needs": needs,
            }
        },
    }


class _Lcg:
    """A tiny deterministic PRNG (no dependency on the global random module, so
    runs are reproducible from the seed alone)."""

    __slots__ = ("_s",)

    def __init__(self, seed: int) -> None:
        self._s = seed & 0xFFFFFFFF

    def below(self, n: int) -> int:
        self._s = (1103515245 * self._s + 12345) & 0x7FFFFFFF
        return self._s % n


def n_swreq_for_total(total: int) -> int:
    """Invert the layer arithmetic: the generator produces ~3.958x n_swreq
    needs in total (sg/8 + fsr/4 + sysreq/4 + req/3 + impl + test)."""
    return max(4, round(total / 3.9583))


def write(path: str | Path, n_swreq: int, seed: int = 1) -> int:
    graph = make_graph(n_swreq, seed)
    Path(path).write_text(json.dumps(graph), encoding="utf-8")
    return graph["versions"]["1.0"]["needs_amount"]


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    out = sys.argv[2] if len(sys.argv) > 2 else f"bench/graph_{n}.json"
    total = write(out, n)
    print(f"wrote {total} needs to {out}")
