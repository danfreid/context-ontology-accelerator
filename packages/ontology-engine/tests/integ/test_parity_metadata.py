# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parity: ``dynamo_store`` returns the same results on Aurora as on DynamoDB (moto).

One scenario runs the public ``dynamo_store`` functions twice. The reference run uses a moto DynamoDB
table; the Aurora run uses ``AuroraTable``/``AuroraDao`` over a real pgvector container. The normalised
results are compared step by step. Timestamps are dropped (they differ per run); everything else must match.
S3 is out of scope and patched out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from coa_ontology import dynamo_store as ds
from coa_ontology.aurora_metadata import AuroraDao, AuroraTable
from moto import mock_aws

pytestmark = pytest.mark.integ

NS = "parity"
TIME_KEYS = ("_at", "_ts", "createdAt", "updatedAt")


def norm(value: Any) -> Any:
    """Drop time-dependent attributes; keep everything else."""
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items() if not str(k).endswith(TIME_KEYS)}
    if isinstance(value, list):
        return [norm(v) for v in value]
    if isinstance(value, tuple):
        return tuple(norm(v) for v in value)
    return value


def by_key(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (i.get("PK", ""), i.get("SK", ""), str(i.get("job_id", ""))))


def capture(fn, *args, **kwargs):
    """Result of ``fn`` or the exception type name."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the scenario compares failures too
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        return f"raised {code or type(exc).__name__}"


def scenario() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []

    def rec(label: str, value: Any) -> None:
        out.append((label, norm(value)))

    # ── ontology registry ───────────────────────────────────────────
    for i in range(5):
        ds.put_ontology_registry(
            NS,
            f"http://ex.org/o{i}",
            {
                "label": f"Onto {i}",
                "uri": f"http://ex.org/o{i}",
                "ontology_type": "domain" if i % 2 == 0 else "upper",
                "domain_tags": ["fin", "risk"] if i < 3 else ["bio"],
                "class_count": Decimal(i),
            },
        )
    ds.put_ontology_registry("other-ns", "http://ex.org/x", {"label": "elsewhere"})
    rec("registry get", ds.get_ontology_registry(NS, "http://ex.org/o1"))
    rec("registry get missing", ds.get_ontology_registry(NS, "http://ex.org/none"))
    rec("registry by uri", ds.get_ontology_registry_by_uri(NS, "http://ex.org/o3"))
    rec("registry list", ds.list_ontologies_registry(NS))
    rec("registry list type", ds.list_ontologies_registry(NS, ontology_type="domain"))
    rec("registry list tag", ds.list_ontologies_registry(NS, domain_tag="fin"))
    rec("registry list limit", ds.list_ontologies_registry(NS, limit=2))
    pages, key, seen = [], None, 0
    while seen < 10:
        items, key = ds.list_ontologies_registry_page(NS, limit=2, exclusive_start_key=key)
        pages.append((items, key))
        seen += 1
        if not key:
            break
    rec("registry pages", pages)
    rec("registry page filtered", ds.list_ontologies_registry_page(NS, attribute_equals={"uri": "http://ex.org/o4"}))
    rec("registry update", ds.update_ontology_registry(NS, "http://ex.org/o1", label="Renamed", status="active"))
    rec("registry update missing", capture(ds.update_ontology_registry, NS, "http://ex.org/none", label="x"))
    rec(
        "registry extend",
        ds.extend_ontology_registry(
            NS, "http://ex.org/o2", add_class_count=3, add_property_count=2, source_proposal="p1"
        ),
    )
    rec(
        "registry extend again",
        ds.extend_ontology_registry(NS, "http://ex.org/o2", add_class_count=1, source_proposal="p2", note="n"),
    )
    rec("registry extend missing", capture(ds.extend_ontology_registry, NS, "http://ex.org/none", add_class_count=1))
    rec("registry delete", ds.delete_ontology_registry(NS, "http://ex.org/o4"))
    rec("registry delete again", ds.delete_ontology_registry(NS, "http://ex.org/o4"))
    rec("registry after delete", ds.list_ontologies_registry(NS))

    # ── namespace meta ──────────────────────────────────────────────
    rec("ns meta absent", ds.get_namespace_meta(NS))
    rec("ns meta put", ds.put_namespace_meta(NS, {"description": "d", "count": Decimal(2)}))
    rec("ns meta get", ds.get_namespace_meta(NS))

    # ── induction lock ──────────────────────────────────────────────
    ds.acquire_induction_lock(NS, "job-a")
    rec("lock second acquire", capture(lambda: ds.acquire_induction_lock(NS, "job-b")))
    try:
        ds.acquire_induction_lock(NS, "job-b")
    except ds.InductionLockHeldError as exc:
        rec("lock holder", str(exc))
    ds.release_induction_lock(NS, "job-b")  # not the holder: left in place
    rec("lock still held", capture(lambda: ds.acquire_induction_lock(NS, "job-c")))
    original = ds._STALE_INDUCTION_LOCK_SECONDS
    ds._STALE_INDUCTION_LOCK_SECONDS = -1000
    try:
        rec("lock stale takeover", capture(lambda: ds.acquire_induction_lock(NS, "job-d")))
    finally:
        ds._STALE_INDUCTION_LOCK_SECONDS = original
    rec("lock after takeover", capture(lambda: ds.acquire_induction_lock(NS, "job-e")))
    ds.release_induction_lock(NS, "job-d")
    rec("lock released by holder", capture(lambda: ds.acquire_induction_lock(NS, "job-f")))
    ds.release_induction_lock(NS)
    rec("lock released unconditionally", capture(lambda: ds.acquire_induction_lock(NS, "job-g")))
    ds.release_induction_lock(NS)
    ds.release_induction_lock(NS)  # idempotent

    # ── induction jobs ──────────────────────────────────────────────
    body = {
        "ontology_uri_prefix": "http://ex.org/",
        "label": "L",
        "datasource_ids": ["d1"],
        "confidence_threshold": 0.9,
    }
    ds.put_job("j1", "queued", body, [{"id": "d1"}], namespace=NS)
    ds.put_job("j2", "queued", body, [{"id": "d2"}], namespace=NS, source_type="UNSTRUCTURED")
    ds.update_job_status("j1", "running", namespace=NS, error=None, progress=Decimal(40))
    ds.touch_job("j1", namespace=NS)
    rec("job get", ds.get_job("j1", namespace=NS))
    rec("job get missing", ds.get_job("zz", namespace=NS))
    rec("jobs list", by_key(ds.list_jobs(NS, limit=50)))
    rec("jobs list status", by_key(ds.list_jobs(NS, status="running")))
    rec("jobs list structured", by_key(ds.list_jobs(NS, source_type="STRUCTURED")))
    rec("jobs list unstructured", by_key(ds.list_jobs(NS, source_type="UNSTRUCTURED")))
    collected, key = [], None
    for _ in range(10):
        items, key = ds.list_jobs_page(NS, limit=1, exclusive_start_key=key)
        collected.extend(items)
        if not key:
            break
    rec("jobs paged", by_key(collected))

    # ── ingest jobs (DAO) ───────────────────────────────────────────
    ds.put_ingest_job("i1", "http://ex.org/o1", "queued", namespace=NS)
    ds.put_ingest_job("i2", "http://ex.org/o2", "queued", namespace=NS)
    ds.update_ingest_job("i1", "completed", namespace=NS, result={"classes": Decimal(3)})
    rec("ingest get", ds.get_ingest_job("i1", namespace=NS))
    rec("ingest get missing", ds.get_ingest_job("zz", namespace=NS))
    rec("ingest list", by_key(ds.list_ingest_jobs(NS)))
    rec("ingest list status", by_key(ds.list_ingest_jobs(NS, status="completed")))

    # ── async jobs (DAO) with the stale sweep ───────────────────────
    ds.put_async_job("VALIDATE", "a1", {"job_id": "a1", "status": "running", "proposal_id": "p1"}, namespace=NS)
    rec("async get", ds.get_async_job("VALIDATE", "a1", namespace=NS))
    ds.update_async_job(
        "VALIDATE", "a1", {"status": "completed", "result": {"ok": True, "n": Decimal(2)}}, namespace=NS
    )
    rec("async updated", ds.get_async_job("VALIDATE", "a1", namespace=NS))
    ds.put_async_job("INFER", "a2", {"job_id": "a2", "status": "running"}, namespace=NS)
    original_stale = ds._STALE_JOB_SECONDS
    ds._STALE_JOB_SECONDS = -1000
    try:
        rec("async swept", ds.get_async_job("INFER", "a2", namespace=NS))
    finally:
        ds._STALE_JOB_SECONDS = original_stale
    rec("async swept persisted", ds.get_async_job("INFER", "a2", namespace=NS))
    rec("async missing", ds.get_async_job("INFER", "zz", namespace=NS))

    # ── proposals ───────────────────────────────────────────────────
    ds.create_proposal_stub("pr1", "j1", NS, "http://ex.org/o1")
    ds.create_proposal_stub("pr2", "j2", NS, "http://ex.org/o2", source_type="UNSTRUCTURED", label="U")
    ds.create_proposal_stub("pr3", "j3", NS, "http://ex.org/o1")
    ds.update_proposal("pr1", NS, status="pending", note="n")
    rec(
        "proposal conditional ok", capture(ds.update_proposal, "pr1", NS, expected_status="pending", status="accepting")
    )
    rec(
        "proposal conditional fail",
        capture(ds.update_proposal, "pr1", NS, expected_status="pending", status="accepting"),
    )
    rec(
        "proposal not-forbidden ok",
        capture(ds.update_proposal, "pr3", NS, expected_status_not="accepted", status="failed"),
    )
    ds.update_proposal("pr3", NS, status="accepted")
    rec(
        "proposal not-forbidden fail",
        capture(ds.update_proposal, "pr3", NS, expected_status_not="accepted", status="failed"),
    )
    rec("proposal update missing", capture(ds.update_proposal, "zz", NS, expected_status="pending", status="x"))
    rec("proposals list", ds.list_proposals(NS, limit=None))
    rec("proposals list status", ds.list_proposals(NS, status="failed"))
    rec("proposals list ontology", ds.list_proposals(NS, ontology_id="http://ex.org/o1", limit=None))
    rec("proposals list type", ds.list_proposals(NS, proposal_type="induction", limit=None))
    rec("proposals list unstructured", ds.list_proposals(NS, source_type="UNSTRUCTURED", limit=None))
    rec("proposals list structured", ds.list_proposals(NS, source_type="STRUCTURED", limit=None))
    rec("proposals list limit", ds.list_proposals(NS, limit=1))
    rec("proposals no namespace", [p["proposal_id"] for p in ds.list_proposals(limit=None)])
    after, ids = None, []
    for _ in range(10):
        items, after = ds.list_proposals_newest_page(NS, limit=1, after=after)
        ids.extend(p["proposal_id"] for p in items)
        if not after:
            break
    rec("proposals newest pages", ids)
    rec("proposal delete", ds.delete_proposal(NS, "pr2"))
    rec("proposal delete again", ds.delete_proposal(NS, "pr2"))

    # ── namespace wipe ──────────────────────────────────────────────
    ds.put_ontology_registry("keep", "http://ex.org/k", {"label": "keep"})
    ds.put_job("keep-job", "queued", body, [], namespace="keep")
    rec("delete all", ds.delete_all_namespace_items(NS))
    rec("delete all again", ds.delete_all_namespace_items(NS))
    rec("after wipe registry", ds.list_ontologies_registry(NS))
    rec("after wipe jobs", ds.list_jobs(NS))
    rec("other ns untouched", ds.get_ontology_registry("keep", "http://ex.org/k"))
    rec("other ns job untouched", ds.get_job("keep-job", namespace="keep"))
    rec("other-ns registry untouched", ds.get_ontology_registry("other-ns", "http://ex.org/x"))
    return out


@pytest.fixture
def no_s3(monkeypatch):
    monkeypatch.setattr(ds, "_get_s3", lambda: MagicMock())
    monkeypatch.setattr(ds, "_best_effort_delete_s3_prefix", lambda *a, **k: None)


def test_dynamo_store_results_match_moto(monkeypatch, no_s3, aurora_client):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("METADATA_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    with mock_aws():
        boto3.client("dynamodb", region_name=ds.REGION).create_table(
            TableName=ds.TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(ds, "_table", None)
        monkeypatch.setattr(ds, "_dao", None)
        reference = scenario()

    table = AuroraTable(client=aurora_client, dimensions=8)
    monkeypatch.setattr(ds, "_table", table)
    monkeypatch.setattr(ds, "_dao", AuroraDao(table))
    actual = scenario()

    assert [label for label, _ in actual] == [label for label, _ in reference]
    for (label, expected), (_, got) in zip(reference, actual, strict=True):
        assert got == expected, label
    assert len(reference) > 60
