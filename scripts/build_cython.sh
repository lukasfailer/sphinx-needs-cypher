#!/usr/bin/env bash
# Build a Cython-compiled copy of the needquery package into build/cython/.
#
# The benchmark's "optimized+cython" tier imports the package with
# PYTHONPATH=build/cython, where the hot modules (graph.py, cypher/optimized.py,
# cypher/parser.py, cypher/ast.py) have been compiled to native extensions from
# the IDENTICAL source — so the measured delta is exactly "what Cython buys",
# nothing else. The pure-Python tree under src/ is untouched.
#
#   ./scripts/build_cython.sh          # uses .venv/bin/python
#   PYTHON=python3 ./scripts/build_cython.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}

if ! "$PY" -c "import Cython" 2>/dev/null; then
  echo "Cython is not installed — run: $PY -m pip install cython setuptools" >&2
  exit 1
fi

rm -rf build/cython
mkdir -p build/cython
cp -r src/needquery build/cython/needquery
find build/cython -name '__pycache__' -type d -prune -exec rm -rf {} +

# Compile the hot path. executor.py (the naive reference interpreter) is left
# interpreted on purpose: it is the readable baseline, not a performance tier.
"$PY" -m cython -3 build/cython/needquery/graph.py \
  build/cython/needquery/cypher/ast.py \
  build/cython/needquery/cypher/parser.py \
  build/cython/needquery/cypher/optimized.py

for c in build/cython/needquery/graph.c \
  build/cython/needquery/cypher/ast.c \
  build/cython/needquery/cypher/parser.c \
  build/cython/needquery/cypher/optimized.c; do
  so="${c%.c}.so"
  echo "cc: $c"
  cc -shared -fPIC -O2 -I"$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')" \
    -o "$so" "$c"
done

PYTHONPATH=build/cython "$PY" - <<'EOF'
import needquery.cypher.optimized as m
import needquery.graph as g
assert m.__file__.endswith(".so"), m.__file__
assert g.__file__.endswith(".so"), g.__file__
print("cython build OK:", m.__file__)
EOF
