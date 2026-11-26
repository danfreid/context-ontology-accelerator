"""Replay a benchmark dataset's queries against a deployed COA API and record latency.

Usage:
    python -m tests.benchmark.query_runner --prefix coa-dev \
        --dataset tests/benchmark/results/dataset_X.json --api-endpoint https://<id>.execute-api.<region>.amazonaws.com/dev

Authentication: a bearer token in ``BENCHMARK_ID_TOKEN`` if set, otherwise AWS SigV4 (``execute-api``) from the
default boto3 credentials.
"""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

RESULTS_DIR = Path(__file__).parent / "results"
REQUEST_TIMEOUT_SECONDS = 60


def build_headers(method: str, url: str, body: str | None, region: str) -> dict[str, str]:
    """Return auth headers: a bearer token from the environment, else a SigV4 signature."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("BENCHMARK_ID_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials found; set BENCHMARK_ID_TOKEN or configure AWS credentials")
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    return dict(request.headers)


def run_query(query: dict, api_endpoint: str, region: str) -> dict:
    """Send one query and return its latency, status and response body."""
    url = api_endpoint.rstrip("/") + query["path"]
    body = json.dumps(query["body"]) if "body" in query else None
    params = query.get("params")
    prepared_url = requests.Request(query["method"], url, params=params).prepare().url or url
    headers = build_headers(query["method"], prepared_url, body, region)

    start = time.perf_counter()
    try:
        response = requests.request(
            query["method"], prepared_url, data=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
        status, text = response.status_code, response.text
    except requests.RequestException as exc:
        status, text = 0, str(exc)
    latency_ms = (time.perf_counter() - start) * 1000

    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    return {"id": query["id"], "status": status, "latencyMs": round(latency_ms, 2), "body": parsed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, help="Deployment prefix, e.g. coa-dev")
    parser.add_argument("--dataset", required=True, type=Path, help="Dataset file from dataset_generator")
    parser.add_argument("--api-endpoint", required=True, help="API Gateway base URL including the stage")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text())
    results = [run_query(q, args.api_endpoint, args.region) for q in dataset["queries"]]

    run = {
        "prefix": args.prefix,
        "dataset": args.dataset.name,
        "apiEndpoint": args.api_endpoint,
        "ranAt": datetime.now(UTC).isoformat(),
        "results": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"run_{args.prefix}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(run, indent=2))
    failed = sum(1 for r in results if not 200 <= r["status"] < 300)
    print(f"Wrote {out} ({len(results)} queries, {failed} non-2xx)")


if __name__ == "__main__":
    main()
