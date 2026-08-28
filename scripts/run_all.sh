#!/usr/bin/env bash
# One command to reproduce every claim in this repo, in order.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then PY="uv run python"; else PY=python3; fi
fi

echo "############ 1. unit tests (engine, shim) ############"
$PY -m pytest tests/ -q

echo; echo "############ 2. Cypher vs Python, on the real demo graph ############"
$PY examples/compare.py

echo; echo "############ 3. parity — reference engine == ubc engine ############"
# needs the ubc binary + the demo checkout; skips the ubc leg if absent
$PY scripts/parity_check.py data/needs.ubc.json

echo; echo "############ 4. security — Python selection is a code-exec surface ############"
$PY examples/security_rce_poc.py

echo; echo "############ 5. migration shim — filter_string -> Cypher ############"
$PY -m needquery.shim

echo; echo "############ 6. benchmark — all engine tiers, all sizes ############"
# Optional Cython tier: build once, the benchmark picks it up automatically.
PYTHON=$PY ./scripts/build_cython.sh || echo "(Cython tier skipped — install cython + a C compiler to enable)"
$PY bench/benchmark.py "$@"   # e.g. ./scripts/run_all.sh --sizes 1000 --repeat 3
