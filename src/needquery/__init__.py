"""needquery — a declarative graph-query surface for Sphinx-Needs.

Two interchangeable backends answer the same openCypher subset:

* :class:`~needquery.backends.reference.ReferenceBackend` — a dependency-free
  pure-Python evaluator.
* :class:`~needquery.backends.ubc.UbcBackend` — shells to the ``ubc`` Rust
  engine when it is present.

Both answer the same query text, so a project can adopt the language first and
swap the engine underneath it later without touching a single query.
"""

from .graph import PropertyGraph, Node
from .backends.reference import ReferenceBackend

__all__ = ["PropertyGraph", "Node", "ReferenceBackend"]
__version__ = "0.1.0"
