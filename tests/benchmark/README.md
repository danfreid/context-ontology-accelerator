# Backend benchmark

Compares a stack on the original backends (Neptune + OpenSearch Serverless + DynamoDB + Fargate) with a stack on the
Aurora backends (Aurora Serverless v2 + pgvector + Lambda). Both are deployed from the same CDK code with the
`graph_backend`, `vector_backend`, `metadata_backend` and `compute_backend` context flags (`make deploy-dev-neptune`,
`make deploy-dev-aurora`).

## Prerequisites

- `uv` and Python 3.12 (`uv run` installs `boto3` and `requests`)
- AWS credentials for the account that holds the deployed stack
- A deployed stack and its API Gateway URL (including the stage)

## Single stack

Run from the repository root:

```bash
tests/benchmark/run_benchmark.sh coa-dev https://<id>.execute-api.<region>.amazonaws.com/dev
```

This writes `dataset_<timestamp>.json` and `run_<prefix>_<timestamp>.json` to `tests/benchmark/results/`
(git-ignored). Set `BENCHMARK_ID_TOKEN` to use a Cognito bearer token. Without it, requests are signed with SigV4.

## Two-stack comparison

Replay the same dataset against both stacks, then compare:

```bash
DATASET=$(ls -t tests/benchmark/results/dataset_*.json | head -1)
uv run python -m tests.benchmark.query_runner --prefix coa-dev --dataset "$DATASET" --api-endpoint <coa-dev-url>
uv run python -m tests.benchmark.query_runner --prefix ckg-dev --dataset "$DATASET" --api-endpoint <ckg-dev-url>
uv run python -m tests.benchmark.compare \
  --run-a tests/benchmark/results/run_coa-dev_<ts>.json \
  --run-b tests/benchmark/results/run_ckg-dev_<ts>.json
```

`compare` prints a markdown table of per-query latency and exits 1 if a query that was correct on run A is
incorrect on run B.

## Golden answers

`golden_answers.json` maps a query id to `{ "expected": <result count>, "tolerance": <allowed difference> }`. Without an
entry, a query counts as correct when it returns 2xx.
