"""Generate a synthetic benchmark dataset for a deployed COA stack.

Usage:
    python -m tests.benchmark.dataset_generator --prefix coa-dev --count 100

The namespaces table name is read from SSM (``/<prefix>/namespace/namespaces-table-name``) and recorded in
the output. The output holds synthetic namespace / source / table / column records and the query list that
``query_runner`` replays. Nothing is written to the deployed stack.
"""

import argparse
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3

RESULTS_DIR = Path(__file__).parent / "results"

DOMAINS = ("trading", "counterparty", "contract", "settlement", "logistics")
COLUMN_TEMPLATES = (
    ("id", "string"),
    ("name", "string"),
    ("created_at", "timestamp"),
    ("amount", "decimal"),
    ("status", "string"),
)


def ssm_prefix(prefix: str) -> str:
    """Return the SSM path prefix for a deployment prefix such as ``coa-dev``.

    The SSM prefix is the resource prefix without the environment suffix (``coa-dev`` -> ``/coa``).
    """
    return "/" + prefix.rsplit("-", 1)[0]


def discover_table_name(prefix: str) -> str:
    """Read the namespaces DynamoDB table name from SSM."""
    ssm = boto3.client("ssm")
    name = f"{ssm_prefix(prefix)}/namespace/namespaces-table-name"
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def make_namespace(index: int) -> dict:
    """Build one synthetic namespace with sources, tables and columns."""
    domain = DOMAINS[index % len(DOMAINS)]
    namespace_id = f"bench-{domain}-{index:04d}"
    tables = []
    for t in range(3):
        table_name = f"{domain}_{t}"
        tables.append(
            {
                "tableId": str(uuid.uuid4()),
                "name": table_name,
                "columns": [{"name": f"{table_name}_{c}", "type": ctype} for c, ctype in COLUMN_TEMPLATES],
            }
        )
    return {
        "namespaceId": namespace_id,
        "domain": domain,
        "sources": [{"sourceId": f"{namespace_id}-src", "type": "DATABASE", "tables": tables}],
    }


def make_queries(namespaces: list[dict]) -> list[dict]:
    """Build the query list: a graph search and an embedding search per namespace."""
    queries = []
    for ns in namespaces:
        nid = ns["namespaceId"]
        term = ns["domain"]
        queries.append(
            {
                "id": f"{nid}-graph-search",
                "method": "GET",
                "path": f"/namespaces/{nid}/graph/search",
                "params": {"q": term},
            }
        )
        queries.append(
            {
                "id": f"{nid}-embedding-search",
                "method": "POST",
                "path": f"/namespaces/{nid}/embeddings/search",
                "body": {"query": f"{term} records", "limit": 10},
            }
        )
    return queries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, help="Deployment prefix, e.g. coa-dev")
    parser.add_argument("--count", type=int, default=100, help="Number of namespaces to generate")
    args = parser.parse_args()

    namespaces = [make_namespace(i) for i in range(args.count)]
    dataset = {
        "prefix": args.prefix,
        "generatedAt": datetime.now(UTC).isoformat(),
        "namespacesTable": discover_table_name(args.prefix),
        "namespaces": namespaces,
        "queries": make_queries(namespaces),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"dataset_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(dataset, indent=2))
    print(f"Wrote {out} ({len(namespaces)} namespaces, {len(dataset['queries'])} queries)")


if __name__ == "__main__":
    main()
