#!/usr/bin/env bash
# Runs the same checks as CI (.github/workflows/tests.yml), fully offline.
# Only CI's Docker smoke test is left out. Stops with a non-zero exit code as soon as any step fails.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWR_FORCE_MOCK=1   # all provider calls use the deterministic mock: no keys, no cost

PY=python3; BIN=""
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; BIN=.venv/bin/; fi
for tool in ruff mypy; do
  command -v "${BIN}${tool}" >/dev/null 2>&1 || {
    echo "MISSING: ${tool}. CI runs it, so install the dev tools first:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev,mcp,web]'"
    exit 1
  }
done

echo "== 1/5 test suite =="
"$PY" -m unittest discover -s tests -v

echo "== 2/5 offline benchmark smoke run =="
"$PY" run_benchmark.py

echo "== 3/5 ruff =="
"${BIN}ruff" check .

echo "== 4/5 mypy =="
"${BIN}mypy"

echo "== 5/5 web API tests =="
"$PY" -m unittest tests.test_web -v

echo "All checks passed."
