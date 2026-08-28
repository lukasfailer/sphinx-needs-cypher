"""Demonstration: node selection with Python is a code-execution surface at
build time; node selection with Cypher is not.

The case states it plainly — ``filter_code`` is "arbitrary embedded Python" and
``filter_string`` is "a Python expression evaluated per need." Anything that can
evaluate a document-supplied Python string executes whatever that string says
when the docs are built. In a regulated toolchain the docs are often built in
CI, from branches, by many contributors — the exact place you do not want
arbitrary code hiding in a ``:filter:`` option.

This script shows two benign payloads (read an environment variable; write a
file) running through the Python path, then shows the same strings going nowhere
through the Cypher parser: they are not valid queries, and even a *valid* Cypher
query has no grammar production that calls a function or imports a module.

Benign by construction: payloads only read one env var and write one file under
a temp dir, then clean up. Nothing here is destructive.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from needquery.cypher.parser import parse, CypherSyntaxError  # noqa: E402


def python_filter_string_executes(expr: str, need: dict) -> object:
    """This is the Sphinx-Needs ``filter_string`` mechanism: eval the document-
    supplied expression with the need as locals. Whatever the expression does,
    happens."""
    return eval(expr, {"__builtins__": __builtins__}, dict(need))  # noqa: S307


def main() -> int:
    marker = os.path.join(tempfile.gettempdir(), "needquery_rce_marker.txt")
    if os.path.exists(marker):
        os.remove(marker)
    os.environ["FAKE_CI_SECRET"] = "tkn-abc123-not-a-real-secret"

    print("== Python path (filter_string / filter_code) ==")
    # Payload 1: exfiltrate an environment variable via a filter expression.
    stolen = python_filter_string_executes(
        "__import__('os').environ.get('FAKE_CI_SECRET')", {"type": "req"}
    )
    print(f"  filter_string read a CI env var at build time: {stolen!r}")

    # Payload 2: write a file from inside a per-need filter expression.
    python_filter_string_executes(
        "open(__import__('os').path.join("
        "__import__('tempfile').gettempdir(), 'needquery_rce_marker.txt'), 'w')"
        ".write('written from a needtable :filter: option')",
        {"type": "req"},
    )
    wrote = os.path.exists(marker)
    print(f"  filter_string wrote a file at build time: {wrote}  ({marker})")
    if wrote:
        os.remove(marker)

    print("\n== Cypher path (parsed, not evaluated) ==")
    for payload in [
        "__import__('os').system('id')",
        "open('/etc/passwd').read()",
    ]:
        try:
            parse(payload)
            print(f"  UNEXPECTED: parsed {payload!r} as a query")
            return 1
        except CypherSyntaxError as exc:
            print(f"  rejected at parse: {payload!r}\n      -> {exc}")

    # Even a well-formed query cannot execute code: the grammar has no function
    # call, no attribute call, no import. The worst a hostile query can do is be
    # expensive — and a read-only engine can bound that.
    q = parse("MATCH (r:req) WHERE r.status = 'open' RETURN r")
    print(f"  a valid query is inert data: {type(q).__name__} with "
          f"{len(q.returns)} return item(s), no code path to a syscall")

    print("\nVerdict: the Python selection path executes document-controlled code "
          "at build time; the Cypher path cannot express code at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
