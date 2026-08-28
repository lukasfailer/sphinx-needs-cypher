"""Measure incremental index persistence against a full cold build.

Three timings per graph size (best of 5, wall clock):

* **full**   — ``PropertyGraph.from_needs_json``: parse + build every index.
* **1 change** — ``IncrementalGraphStore.load`` with a warm cache and exactly
  one changed need (the cache is restored to its pristine copy before every
  repetition, so each run really applies one delta and re-saves).
* **noop**   — ``IncrementalGraphStore.load`` with a warm cache and an
  unchanged file: hash + diff, zero deltas, no re-save.

The incremental path still pays for parsing the whole ``needs.json`` and
hashing every need — that is the honest floor of a file-granular input; the
win is skipping index construction. Results go to
``bench/results/incremental.json``.

Usage: ``.venv/bin/python bench/bench_incremental.py``
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import generate  # noqa: E402  (bench/generate.py)
from needquery import PropertyGraph  # noqa: E402
from needquery.incremental import IncrementalGraphStore  # noqa: E402

SIZES = [10_000, 40_000]
REPS = 5


def best_of(reps: int, fn, setup=None) -> float:
    """Best wall-clock time of ``fn`` over ``reps`` runs; ``setup`` runs
    before each rep OUTSIDE the timed region (cache restore is harness work,
    not engine work)."""
    best = float("inf")
    for _ in range(reps):
        if setup is not None:
            setup()
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def bench_size(total: int, workdir: Path) -> dict:
    needs_json = workdir / f"needs_{total}.json"
    n_needs = generate.write(needs_json, generate.n_swreq_for_total(total))

    # full cold build (no cache involved)
    t_full = best_of(REPS, lambda: PropertyGraph.from_needs_json(needs_json))

    # warm the cache once, keep a pristine copy to restore between reps
    cache = workdir / f"index_{total}.cache"
    pristine = workdir / f"index_{total}.cache.pristine"
    IncrementalGraphStore(cache).load(needs_json)
    shutil.copyfile(cache, pristine)

    # exactly one changed need
    doc = json.loads(needs_json.read_text(encoding="utf-8"))
    needs = doc["versions"]["1.0"]["needs"]
    needs["SWREQ_0"]["status"] = "changed-for-bench"
    mutated_json = workdir / f"needs_{total}_mut.json"
    mutated_json.write_text(json.dumps(doc), encoding="utf-8")

    def one_change():
        store = IncrementalGraphStore(cache)
        store.load(mutated_json)
        assert store.last_stats == {
            "mode": "incremental", "added": 0, "removed": 0, "changed": 1,
            "rebuilt": 1,
        }

    t_one = best_of(REPS, one_change,
                    setup=lambda: shutil.copyfile(pristine, cache))

    # no-op reload: file unchanged, cache stays valid between reps
    shutil.copyfile(pristine, cache)

    def noop():
        store = IncrementalGraphStore(cache)
        store.load(needs_json)
        assert store.last_stats["mode"] == "noop"

    t_noop = best_of(REPS, noop)

    # Component breakdown: where a 1-change reload's time actually goes.
    # This is the point of the measurement — in pure Python the floor
    # (parse + hash + cache read) sits at full-build cost already.
    from needquery.incremental import IncrementalGraphStore as S, need_hash

    raw = json.loads(mutated_json.read_text(encoding="utf-8"))
    needs = raw["versions"]["1.0"]["needs"]
    breakdown = {
        "json_parse_s": best_of(3, lambda: json.loads(mutated_json.read_text(encoding="utf-8"))),
        "link_fields_s": best_of(3, lambda: PropertyGraph._link_fields(None, needs)),
        "hash_all_needs_s": best_of(3, lambda: {k: need_hash(d) for k, d in needs.items()}),
        "cache_read_s": best_of(3, lambda: S._graph_from_cache(S(cache)._read_cache())),
    }
    breakdown = {k: round(v, 4) for k, v in breakdown.items()}

    return {
        "needs": n_needs,
        "breakdown_1_change": breakdown,
        "full_build_s": round(t_full, 4),
        "one_change_reload_s": round(t_one, 4),
        "noop_reload_s": round(t_noop, 4),
        "speedup_one_change": round(t_full / t_one, 2),
        "speedup_noop": round(t_full / t_noop, 2),
    }


def main() -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="needquery-inc-bench-") as td:
        for total in SIZES:
            results.append(bench_size(total, Path(td)))

    out = {
        "date": date.today().isoformat(),
        "reps": REPS,
        "note": "best-of wall clock; incremental numbers include JSON parse "
                "+ per-need hashing (the file-granular floor) and, for the "
                "1-change case, the cache re-save",
        "results": results,
    }
    out_path = HERE / "results" / "incremental.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    hdr = f"{'needs':>8} {'full build':>12} {'1-change':>12} {'noop':>12} {'x(1-chg)':>9} {'x(noop)':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(
            f"{r['needs']:>8} {r['full_build_s']:>11.3f}s {r['one_change_reload_s']:>11.3f}s "
            f"{r['noop_reload_s']:>11.3f}s {r['speedup_one_change']:>8.1f}x {r['speedup_noop']:>8.1f}x"
        )
    print("\n1-change floor (parse + link-field scan + hash + cache read):")
    for r in results:
        b = r["breakdown_1_change"]
        floor = sum(b.values())
        print(
            f"{r['needs']:>8}  parse {b['json_parse_s']:.3f}s + link-fields "
            f"{b['link_fields_s']:.3f}s + hash {b['hash_all_needs_s']:.3f}s + "
            f"cache-read {b['cache_read_s']:.3f}s = {floor:.3f}s "
            f"(full build: {r['full_build_s']:.3f}s)"
        )
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
