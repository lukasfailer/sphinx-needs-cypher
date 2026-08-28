"""The LLM experiment: is the selection *actually* easier for an LLM in Cypher?

12 natural-language selection tasks on the real demo graph. For each task an LLM
(called through the `claude` CLI, model recorded in the results) writes BOTH:

  * a Sphinx-Needs ``filter_string`` (the OSS status quo), and
  * an openCypher query (the declarative alternative),

from the *identical* schema description. Both answers are executed — the
filter_string with the same eval-per-need semantics sphinx-needs uses, the
Cypher against the reference engine — and scored against a ground truth that is
computed independently in plain Python right here in this file.

    PYTHONPATH=src:examples python3 bench/llm_eval.py

Writes bench/results/llm_eval.json with every raw model answer, so the numbers
on the slide are reproducible and auditable.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

import status_quo as sq  # noqa: E402
from needquery import PropertyGraph, ReferenceBackend  # noqa: E402

MODEL = "claude-sonnet-5"
DATA = ROOT / "data" / "needs.ubc.json"

SCHEMA = """\
The project is a Sphinx-Needs requirements graph with 292 items ("needs").
Each need has these attributes: id (string), type (one of: safety_goal, fsr,
sysreq, req, swreq, impl, test, swarch, arch, interface, component, seq_msg,
need, person, layer, release, team, spec, hazard), title (string), status
('open', 'closed', 'in progress', 'passed', 'failed', or missing), asil ('D'
or missing), tags (list of strings, possibly empty).
Typed, directed link fields (each a list of target need ids): links,
parent_needs, author, based_on, implements, depends_on, realizes, spec, specs,
runs, mitigates, reqs, derives_from, consumes, provides, provided_by, uses,
startup_calls, shutdown_calls, release, persons.
For every link field X, every need also carries the automatic reverse field
X_back: the list of ids of needs whose X points at this need."""

FS_INSTR = """\
Write a Sphinx-Needs filter_string for the task below.
A filter_string is a single Python boolean expression evaluated once per need;
the need's attributes are available as plain variables (example:
type == 'swreq' and status == 'open'). It sees ONLY the current need's own
fields — no access to other needs, no loops, no functions beyond len/any/all/str.
Reply with ONLY the expression on one line, nothing else — no backticks, no
explanation."""

CY_INSTR = """\
Write an openCypher query for the task below.
The engine supports: MATCH with labelled nodes (label = need type, e.g.
(r:swreq)), directed relationships with optional types (e.g. (a)-[:links]->(b),
multiple types [:links|specs], variable length [*]), WHERE with comparisons on
properties (n.status = 'open', n.id = 'X', 'safety' IN n.tags), boolean
operators, and pattern predicates (e.g. WHERE NOT ( ()-->(r) )).
End the query with: RETURN <variable> — return the matching need nodes.
Reply with ONLY the query, nothing else — no backticks, no explanation."""


def gt(needs: dict) -> dict[str, tuple[str, set[str], bool]]:
    """key -> (natural-language task, ground-truth id set, expressible as
    filter_string?). Ground truths are computed here in plain dict code — not
    with the engine — so the engine is graded, never trusted."""
    L = sq.LINK_FIELDS

    def incoming(nid_set_type=None):
        out: dict[str, list[str]] = {}
        for nid, n in needs.items():
            srcs = [s for f in L for s in (n.get(f + "_back") or [])]
            out[nid] = srcs
        return out

    inc = incoming()

    reach_sg01: set[str] = set()
    frontier = ["SG_01"]
    while frontier:
        nxt = []
        for tgt in frontier:
            for src in inc.get(tgt, []):
                if src not in reach_sg01:
                    reach_sg01.add(src)
                    nxt.append(src)
        frontier = nxt

    return {
        "open_swreqs": (
            "All software requirements (type swreq) with status 'open'.",
            {i for i, n in needs.items()
             if n.get("type") == "swreq" and n.get("status") == "open"},
            True),
        "asil_d_goals": (
            "All safety goals (type safety_goal) rated asil 'D'.",
            {i for i, n in needs.items()
             if n.get("type") == "safety_goal" and n.get("asil") == "D"},
            True),
        "tagged_safety": (
            "All needs (any type) that carry the tag 'safety'.",
            {i for i, n in needs.items() if "safety" in (n.get("tags") or [])},
            True),
        "all_closed": (
            "All needs (any type) with status 'closed'.",
            {i for i, n in needs.items() if n.get("status") == "closed"},
            True),
        "impl_nonempty": (
            "All implementations (type impl) that implement at least one need "
            "(their implements list is non-empty).",
            {i for i, n in needs.items()
             if n.get("type") == "impl" and (n.get("implements") or [])},
            True),
        "tests_of_swreq13": (
            "All tests (type test) whose links field contains SWREQ_013.",
            {i for i, n in needs.items()
             if n.get("type") == "test" and "SWREQ_013" in (n.get("links") or [])},
            True),
        "swarch002_targets": (
            "All needs that SWARCH_002 points at through its links field.",
            {t for t in (needs["SWARCH_002"].get("links") or []) if t in needs},
            True),
        "fsr_from_sg01": (
            "All functional safety requirements (type fsr) that derive "
            "directly from the safety goal SG_01 (via derives_from).",
            {i for i, n in needs.items()
             if n.get("type") == "fsr" and "SG_01" in (n.get("derives_from") or [])},
            True),
        "untraced_swreqs": (
            "All software requirements (type swreq) that nothing links to: no "
            "other need points at them through ANY link type.",
            {i for i, n in needs.items()
             if n.get("type") == "swreq" and not inc[i]},
            True),
        "swreq_no_test": (
            "All software requirements (type swreq) that no test (type test) "
            "points at through any link type.",
            {i for i, n in needs.items()
             if n.get("type") == "swreq"
             and not any(needs.get(s, {}).get("type") == "test" for s in inc[i])},
            False),
        "sysreq_to_sg01": (
            "All system requirements (type sysreq) that implement a functional "
            "safety requirement (type fsr) which derives directly from the "
            "safety goal SG_01: sysreq -implements-> fsr -derives_from-> SG_01.",
            {i for i, n in needs.items()
             if n.get("type") == "sysreq" and any(
                 needs.get(f, {}).get("type") == "fsr"
                 and "SG_01" in (needs.get(f, {}).get("derives_from") or [])
                 for f in (n.get("implements") or []))},
            False),
        "reach_sg01": (
            "All needs from which the safety goal SG_01 is reachable through "
            "one or more directed links of any type (direct or transitive).",
            reach_sg01,
            False),
    }


def ask(prompt: str, scratch: Path) -> str:
    out = subprocess.run(
        ["claude", "-p", "--model", MODEL, prompt],
        capture_output=True, text=True, cwd=scratch, timeout=240)
    if out.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {out.stderr[:400]}")
    return out.stdout.strip()


def clean(ans: str) -> str:
    ans = ans.strip()
    ans = re.sub(r"^```[a-z]*\n?", "", ans)
    ans = re.sub(r"\n?```$", "", ans)
    return ans.strip()


def main() -> None:
    needs = sq.load_needs(DATA)
    graph = PropertyGraph.from_needs_json(DATA)
    cy = ReferenceBackend(graph)
    tasks = gt(needs)
    scratch = Path("/tmp")

    results = []
    for key, (nl, truth, fs_possible) in tasks.items():
        row: dict = {"key": key, "task": nl, "truth_n": len(truth),
                     "fs_expressible": fs_possible}
        print(f"== {key} ({len(truth)} expected)")

        fs_ans = clean(ask(f"{SCHEMA}\n\n{FS_INSTR}\n\nTask: {nl}", scratch))
        row["filter_string"] = fs_ans
        try:
            got = set(sq.filter_string(needs, fs_ans))
            row["fs_correct"] = got == truth
            row["fs_n"] = len(got)
        except Exception as e:  # noqa: BLE001
            row["fs_correct"] = False
            row["fs_error"] = repr(e)
        print(f"   fs : {row.get('fs_correct')}  {fs_ans[:90]}")

        cy_ans = clean(ask(f"{SCHEMA}\n\n{CY_INSTR}\n\nTask: {nl}", scratch))
        row["cypher"] = cy_ans
        m = re.search(r"RETURN\s+(?:DISTINCT\s+)?(\w+)", cy_ans, re.I)
        try:
            var = m.group(1) if m else "n"
            got = set(cy.select_ids(cy_ans, var))
            row["cy_correct"] = got == truth
            row["cy_n"] = len(got)
        except Exception as e:  # noqa: BLE001
            row["cy_correct"] = False
            row["cy_error"] = repr(e)
        print(f"   cy : {row.get('cy_correct')}  {cy_ans[:90]}")
        results.append(row)

    summary = {
        "model": MODEL,
        "tasks": len(results),
        "fs_correct": sum(r["fs_correct"] for r in results),
        "cy_correct": sum(r["cy_correct"] for r in results),
        "fs_inexpressible": sum(not r["fs_expressible"] for r in results),
        "rows": results,
    }
    out = ROOT / "bench" / "results" / "llm_eval.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nCypher {summary['cy_correct']}/{summary['tasks']} · "
          f"filter_string {summary['fs_correct']}/{summary['tasks']} "
          f"(of which {summary['fs_inexpressible']} tasks are not expressible "
          f"as a filter_string at all)\n-> {out}")


if __name__ == "__main__":
    main()
