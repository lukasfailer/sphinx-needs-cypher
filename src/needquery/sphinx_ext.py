"""A Sphinx directive that selects needs with a Cypher query.

    .. needquery::
       :query: MATCH (r:swreq) WHERE NOT ( ()-->(r) ) RETURN r
       :columns: id, title, status

It renders a table of the matched needs — the declarative analogue of a
``needtable`` whose ``:filter:`` is a Python string. The point of shipping it as
a real directive is to show the whole surface end to end: the *language* in the
docs, backed by the pure-Python reference engine, with the exact same query text
that also runs against the faster ``ubc`` engine.

Selection runs at ``doctree-resolved`` time, after Sphinx-Needs has collected
every need, so the graph the query sees is complete.
"""

from __future__ import annotations

from typing import Any

from docutils import nodes
from docutils.parsers.rst import Directive, directives

from .graph import PropertyGraph
from .backends.reference import ReferenceBackend


class needquery_placeholder(nodes.General, nodes.Element):
    """Inserted at parse time; replaced with a table at resolve time."""


class NeedQueryDirective(Directive):
    has_content = False
    required_arguments = 0
    optional_arguments = 0
    option_spec = {
        "query": directives.unchanged_required,
        "columns": directives.unchanged,
    }

    def run(self) -> list[nodes.Node]:
        node = needquery_placeholder()
        node["query"] = self.options["query"]
        node["columns"] = [
            c.strip() for c in self.options.get("columns", "id, title").split(",")
        ]
        return [node]


def _collect_needs(app) -> dict[str, dict[str, Any]]:
    """Pull the finished needs mapping out of Sphinx-Needs, tolerant of the
    couple of accessor shapes different versions expose."""
    env = app.builder.env
    try:  # modern Sphinx-Needs
        from sphinx_needs.data import SphinxNeedsData

        view = SphinxNeedsData(env).get_needs_view()
        return {nid: dict(view[nid]) for nid in view}
    except Exception:
        needs = getattr(env, "needs_all_needs", {})
        return {nid: dict(data) for nid, data in needs.items()}


def _resolve(app, doctree, _docname) -> None:
    placeholders = list(doctree.findall(needquery_placeholder))
    if not placeholders:
        return
    needs = _collect_needs(app)
    link_fields = PropertyGraph._link_fields(None, needs)
    graph = PropertyGraph.from_needs(needs, link_fields)
    backend = ReferenceBackend(graph)

    for ph in placeholders:
        ids = backend.select_ids(ph["query"])
        ph.replace_self(_render_table(ph["query"], ph["columns"], ids, needs))


def _render_table(query: str, columns: list[str], ids: list[str],
                  needs: dict[str, dict]) -> nodes.Node:
    container = nodes.container()
    caption = nodes.paragraph()
    caption += nodes.emphasis(text=f"needquery: {query}  ({len(ids)} matched)")
    container += caption

    table = nodes.table()
    tgroup = nodes.tgroup(cols=len(columns))
    table += tgroup
    for _ in columns:
        tgroup += nodes.colspec(colwidth=1)
    thead = nodes.thead()
    tgroup += thead
    hrow = nodes.row()
    for col in columns:
        entry = nodes.entry()
        entry += nodes.paragraph(text=col)
        hrow += entry
    thead += hrow
    tbody = nodes.tbody()
    tgroup += tbody
    for nid in ids:
        need = needs.get(nid, {})
        row = nodes.row()
        for col in columns:
            entry = nodes.entry()
            value = nid if col == "id" else need.get(col, "")
            entry += nodes.paragraph(text=str(value if value is not None else ""))
            row += entry
        tbody += row
    container += table
    return container


def setup(app) -> dict[str, Any]:
    app.add_node(needquery_placeholder)
    app.add_directive("needquery", NeedQueryDirective)
    # priority 800 runs after Sphinx-Needs (500) has populated the graph.
    app.connect("doctree-resolved", _resolve, priority=800)
    return {"version": "0.1.0", "parallel_read_safe": True,
            "parallel_write_safe": True}
