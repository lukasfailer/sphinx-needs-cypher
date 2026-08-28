"""Minimal Sphinx project proving the needquery directive renders in a real
build next to a native Sphinx-Needs needtable."""
import os
import sys

sys.path.insert(0, os.path.abspath("../../src"))

project = "needquery demo"
author = "case study"
extensions = ["sphinx_needs", "needquery.sphinx_ext"]
needs_types = [
    {"directive": "req", "title": "Requirement", "prefix": "R_"},
    {"directive": "swreq", "title": "SW Requirement", "prefix": "SR_"},
    {"directive": "test", "title": "Test", "prefix": "T_"},
]
needs_extra_links = [
    {"option": "links", "incoming": "linked by", "outgoing": "links to"},
]
html_theme = "basic"
