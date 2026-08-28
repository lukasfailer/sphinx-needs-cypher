#!/usr/bin/env bash
# Benchmark-only entry point — run the 9-lane benchmark without the rest of
# scripts/run_all.sh, with the interpreter and PYTHONPATH handled for you.
#
#   ./scripts/bench.sh                 # full benchmark, default sizes, best of 5
#   ./scripts/bench.sh quick           # 1 000 needs, 3 repeats — a fast sanity pass
#   ./scripts/bench.sh --sizes 300 40000 --repeat 5     # any bench/benchmark.py args
#   ./scripts/bench.sh --cython        # build the Cython tier first, then benchmark
#   ./scripts/bench.sh --plot          # regenerate the charts afterwards
#   ./scripts/bench.sh incremental     # the incremental-loading benchmark instead
#   ./scripts/bench.sh llm             # the LLM query-writing experiment instead
#
# Interpreter: $PYTHON if set, else .venv/bin/python, else `uv run python`, else python3.
# (The Cython tier needs an interpreter that has Cython — the .venv does.)
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python
  elif command -v uv >/dev/null 2>&1; then PY="uv run python"
  else PY=python3; fi
fi

build_cython=0
plot=0
args=()
for a in "$@"; do
  case "$a" in
    quick)       args+=(--sizes 1000 --repeat 3) ;;
    --cython)    build_cython=1 ;;
    --plot)      plot=1 ;;
    incremental) exec env PYTHONPATH=src "$PY" bench/bench_incremental.py ;;
    llm)         exec env PYTHONPATH=src:examples "$PY" bench/llm_eval.py ;;
    *)           args+=("$a") ;;
  esac
done

if [ "$build_cython" = 1 ]; then
  PYTHON=$PY ./scripts/build_cython.sh || echo "(Cython build failed — continuing without that tier)"
fi

PYTHONPATH=src "$PY" bench/benchmark.py "${args[@]}"

if [ "$plot" = 1 ]; then
  PYTHONPATH=src "$PY" bench/plot.py
fi
