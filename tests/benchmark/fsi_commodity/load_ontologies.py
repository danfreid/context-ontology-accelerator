# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Load the pinned ontology snapshots in ``ontologies/manifest.yaml`` into a namespace.

Two targets:

* A deployed stack, through the public API (presigned S3 upload, then ingest-from-s3, then poll
  ingest-status)::

      python -m tests.benchmark.fsi_commodity.load_ontologies \\
          --api-endpoint https://<id>.execute-api.<region>.amazonaws.com/dev --namespace-id <uuid>

  Auth matches ``tests.benchmark.query_runner``: ``BENCHMARK_ID_TOKEN`` as a bearer token if set,
  otherwise SigV4 from the default AWS credentials.

* A local ontology engine (the demo's ``uvicorn coa_ontology.main:app``), through its direct upload
  route::

      python -m tests.benchmark.fsi_commodity.load_ontologies --engine-url http://localhost:8001 --namespace default

Every file's SHA-256 is checked against the manifest before anything is sent. Ontologies that already
exist in the namespace are skipped, so a rerun only loads what is missing. ``--dry-run`` checks the
files and prints the plan without network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ONTOLOGY_DIR = Path(__file__).resolve().parent / "ontologies"
MANIFEST_PATH = ONTOLOGY_DIR / "manifest.yaml"
TURTLE = "text/turtle"
REQUEST_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 5.0


class ManifestError(ValueError):
    """A snapshot file is missing, or its bytes do not match the manifest."""


@dataclass(frozen=True)
class Snapshot:
    """One manifest entry resolved against the directory that holds the files."""

    key: str
    path: Path
    ontology_id: str
    title: str
    ontology_type: str
    sha256: str
    size: int
    optional: bool
    domain_tags: tuple[str, ...]
    license: str
    source: str

    @property
    def client_token(self) -> str:
        """Idempotency token for ingest-from-s3: stable for the same file, at most 64 characters."""
        return f"{self.key}-{self.sha256[:16]}"[:64]


def load_manifest(manifest_path: Path = MANIFEST_PATH, base_dir: Path | None = None) -> list[Snapshot]:
    """Read the manifest and return its entries in file order."""
    base = base_dir or manifest_path.parent
    with manifest_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return [
        Snapshot(
            key=e["key"],
            path=base / e["file"],
            ontology_id=e["ontology_id"],
            title=e["title"],
            ontology_type=e["ontology_type"],
            sha256=e["sha256"],
            size=int(e["bytes"]),
            optional=bool(e.get("optional", False)),
            domain_tags=tuple(e.get("domain_tags") or ()),
            license=str(e.get("license", "")),
            source=str(e.get("source", "")),
        )
        for e in raw["ontologies"]
    ]


def select(
    snapshots: list[Snapshot], only: list[str] | None = None, include_optional: bool = False
) -> tuple[list[Snapshot], list[str]]:
    """Return the snapshots to load and a list of skip notes.

    ``only`` restricts the set to those keys (unknown keys raise). Optional snapshots are loaded only
    with ``include_optional`` and when their file is present.
    """
    known = {s.key for s in snapshots}
    if only:
        unknown = sorted(set(only) - known)
        if unknown:
            raise ManifestError(f"unknown snapshot key(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    chosen, notes = [], []
    for s in snapshots:
        if only and s.key not in only:
            continue
        if s.optional and not include_optional:
            notes.append(f"{s.key}: optional, skipped (pass --include-optional)")
            continue
        if s.optional and not s.path.exists():
            notes.append(f"{s.key}: optional and {s.path.name} is not present, skipped")
            continue
        chosen.append(s)
    return chosen, notes


def read_verified(snapshot: Snapshot) -> bytes:
    """Return the file's bytes after checking size and SHA-256 against the manifest."""
    if not snapshot.path.exists():
        raise ManifestError(f"{snapshot.key}: {snapshot.path} does not exist")
    data = snapshot.path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != snapshot.size or digest != snapshot.sha256:
        raise ManifestError(
            f"{snapshot.key}: {snapshot.path.name} has {len(data)} bytes, sha256 {digest}; "
            f"manifest expects {snapshot.size} bytes, sha256 {snapshot.sha256}"
        )
    return data


def _filename(snapshot: Snapshot) -> str:
    """Upload filename, sanitised the way the web app does it."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in snapshot.ontology_id) + ".ttl"


def auth_headers(method: str, url: str, body: str | None, region: str) -> dict[str, str]:
    """Auth headers, as in ``tests.benchmark.query_runner``: a bearer token if set, else SigV4."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("BENCHMARK_ID_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials found; set BENCHMARK_ID_TOKEN or configure AWS credentials")
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    return dict(request.headers)


class DeployedTarget:
    """Public API of a deployed stack: upload-url, S3 PUT, ingest-from-s3, ingest-status."""

    def __init__(self, api_endpoint: str, namespace_id: str, region: str, timeout_s: float) -> None:
        uuid.UUID(namespace_id)  # the API types namespaceId as a UUID; fail before any call
        self.base = api_endpoint.rstrip("/") + f"/namespaces/{quote(namespace_id, safe='')}/ontologies"
        self.region = region
        self.timeout_s = timeout_s

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None):
        import requests

        data = json.dumps(body) if body is not None else None
        headers = auth_headers(method, url, data, self.region)
        return requests.request(method, url, data=data, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

    def _url(self, snapshot: Snapshot, suffix: str = "") -> str:
        return f"{self.base}/{quote(snapshot.ontology_id, safe='')}{suffix}"

    def exists(self, snapshot: Snapshot) -> bool:
        resp = self._call("GET", self._url(snapshot))
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def load(self, snapshot: Snapshot, data: bytes) -> dict[str, Any]:
        import requests

        resp = self._call(
            "POST",
            f"{self.base}/upload-url",
            {
                "filename": _filename(snapshot),
                "contentType": TURTLE,
                "ontologyId": snapshot.ontology_id,
                "title": snapshot.title,
            },
        )
        resp.raise_for_status()
        presigned = resp.json()
        put = requests.put(
            presigned["uploadUrl"], data=data, headers={"Content-Type": TURTLE}, timeout=REQUEST_TIMEOUT_SECONDS
        )
        put.raise_for_status()

        resp = self._call(
            "POST",
            self._url(snapshot, "/ingest-from-s3"),
            {
                "s3Key": presigned["s3Key"],
                "format": "turtle",
                "title": snapshot.title,
                "ontologyType": snapshot.ontology_type,
                "clientToken": snapshot.client_token,
            },
        )
        resp.raise_for_status()
        job = resp.json()["result"]

        deadline = time.monotonic() + self.timeout_s
        while job["status"] not in ("completed", "failed"):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{snapshot.key}: ingest job {job['jobId']} still {job['status']}")
            time.sleep(POLL_INTERVAL_SECONDS)
            resp = self._call("GET", self._url(snapshot, f"/ingest-status/{quote(job['jobId'], safe='')}"))
            resp.raise_for_status()
            job = resp.json()["result"]
        if job["status"] == "failed":
            raise RuntimeError(f"{snapshot.key}: ingest failed: {job.get('error')}")
        return job.get("result") or {}


class LocalTarget:
    """A locally running ontology engine: ``POST /ontologies/{id}/upload`` (multipart)."""

    def __init__(self, engine_url: str, namespace: str) -> None:
        self.base = engine_url.rstrip("/") + "/ontologies"
        self.namespace = namespace

    def _url(self, snapshot: Snapshot, suffix: str = "") -> str:
        return f"{self.base}/{quote(snapshot.ontology_id, safe='')}{suffix}"

    def exists(self, snapshot: Snapshot) -> bool:
        import requests

        resp = requests.get(self._url(snapshot), params={"namespace": self.namespace}, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def load(self, snapshot: Snapshot, data: bytes) -> dict[str, Any]:
        import requests

        resp = requests.post(
            self._url(snapshot, "/upload"),
            files={"file": (_filename(snapshot), data, TURTLE)},
            data={
                "namespace": self.namespace,
                "title": snapshot.title,
                "ontology_type": snapshot.ontology_type,
                "format": "turtle",
                "license": snapshot.license,
                "source_url": snapshot.source if snapshot.source.startswith("https://") else "",
                "domain_tags": ",".join(snapshot.domain_tags),
            },
            # Ingest waits for embeddings to be searchable; the largest snapshots take minutes.
            timeout=max(REQUEST_TIMEOUT_SECONDS, 900),
        )
        resp.raise_for_status()
        body = resp.json()
        return {"classCount": body.get("class_count"), "propertyCount": body.get("property_count")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--api-endpoint", help="Deployed API Gateway base URL including the stage")
    target.add_argument("--engine-url", help="Local ontology engine URL, e.g. http://localhost:8001")
    target.add_argument("--dry-run", action="store_true", help="Verify files and print the plan; no network")
    parser.add_argument("--namespace-id", help="Namespace UUID (deployed target)")
    parser.add_argument("--namespace", default="default", help="Namespace name (local target, default: default)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--only", nargs="+", metavar="KEY", help="Load only these manifest keys")
    parser.add_argument("--include-optional", action="store_true", help="Also load optional snapshots if present")
    parser.add_argument("--timeout", type=float, default=900.0, help="Per-ontology ingest timeout in seconds")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args(argv)

    if args.api_endpoint and not args.namespace_id:
        parser.error("--api-endpoint needs --namespace-id")

    snapshots, notes = select(load_manifest(args.manifest), args.only, args.include_optional)
    for note in notes:
        print(f"skip  {note}")
    payloads = {s.key: read_verified(s) for s in snapshots}  # verify everything before the first call
    total = sum(len(p) for p in payloads.values())
    print(f"verified {len(snapshots)} snapshot(s), {total / 1_048_576:.1f} MB")

    if args.dry_run:
        for s in snapshots:
            print(f"plan  {s.key:<24} {s.ontology_type:<13} {s.ontology_id}")
        return 0

    tgt = (
        DeployedTarget(args.api_endpoint, args.namespace_id, args.region, args.timeout)
        if args.api_endpoint
        else LocalTarget(args.engine_url, args.namespace)
    )
    failures = 0
    for s in snapshots:
        try:
            if tgt.exists(s):
                print(f"exists {s.key}")
                continue
            started = time.perf_counter()
            result = tgt.load(s, payloads[s.key])
            print(f"loaded {s.key:<24} {time.perf_counter() - started:6.1f}s  {result}")
        except Exception as exc:  # noqa: BLE001 — report every snapshot, then exit non-zero
            failures += 1
            print(f"FAILED {s.key}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
