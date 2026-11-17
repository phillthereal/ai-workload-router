#!/usr/bin/env bash
# Runs the same checks as CI (.github/workflows/tests.yml), fully offline.
# Stops with a non-zero exit code as soon as any step fails.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWR_FORCE_MOCK=1   # all provider calls use the deterministic mock: no keys, no cost

echo "== 1/2 test suite =="
python3 -m unittest discover -s tests -v

echo "== 2/2 offline benchmark smoke run =="
python3 run_benchmark.py

echo "All checks passed."
