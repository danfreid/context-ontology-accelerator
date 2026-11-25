#!/usr/bin/env bash
# Generate a dataset and replay its queries against one deployed stack.
# Usage: tests/benchmark/run_benchmark.sh <prefix> <api-endpoint>
# Run from the repository root.
set -euo pipefail

PREFIX="${1:-coa-dev}"
API_ENDPOINT="${2:?API_ENDPOINT required}"

python -m tests.benchmark.dataset_generator --prefix "$PREFIX" --count 100
DATASET=$(ls -t tests/benchmark/results/dataset_*.json | head -1)
python -m tests.benchmark.query_runner --prefix "$PREFIX" --dataset "$DATASET" --api-endpoint "$API_ENDPOINT"
