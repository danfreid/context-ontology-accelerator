# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Sources and scan-job writes with ``METADATA_BACKEND=aurora`` over the in-memory Aurora metadata store."""

from __future__ import annotations

import pytest
from coa_common.testing_aurora import install
from coa_sources.database.graph_verify import handler as gv

pytestmark = pytest.mark.unit

NS = "ns-1"
SRC = {"PK": f"NS#{NS}", "SK": "SRC#s1"}
CLAIM = {"PK": "GRAPH_CLUSTER#host", "SK": "CLAIM"}
JOB_SK = "SCAN#2026-01-02T00:00:00Z"


@pytest.fixture
def store(monkeypatch):
    s = install(monkeypatch)
    monkeypatch.setattr(gv, "SOURCES_TABLE", "coa-dev-sources")
    monkeypatch.setattr(gv, "SOURCE_SCAN_JOBS_TABLE", "coa-dev-source-scan-jobs")
    monkeypatch.setattr(gv, "_dao", None)
    monkeypatch.setattr(gv, "_scan_dao", None)
    yield s
    monkeypatch.setattr(gv, "_dao", None)
    monkeypatch.setattr(gv, "_scan_dao", None)


def _seed(store, **fields):
    store["coa-dev-sources"] = {
        (SRC["PK"], SRC["SK"]): {**SRC, "status": "REGISTERED", "errorMessage": "old", **fields}
    }
    store["coa-dev-source-scan-jobs"] = {
        ("SRC#s1", JOB_SK): {"PK": "SRC#s1", "SK": JOB_SK, "status": "IN_PROGRESS", "namespaceId": NS, "createdAt": "x"}
    }


def test_approve_takes_the_claim_and_approves_the_source_in_one_transaction(store):
    _seed(store)
    item = store["coa-dev-sources"][(SRC["PK"], SRC["SK"])]
    assert gv._approve_holding_claim(item, NS, CLAIM, JOB_SK) is None
    source = store["coa-dev-sources"][(SRC["PK"], SRC["SK"])]
    assert (source["status"], source["queryable"], source["lastScanJobId"]) == ("APPROVED", True, JOB_SK)
    assert "errorMessage" not in source
    assert store["coa-dev-sources"][(CLAIM["PK"], CLAIM["SK"])]["namespaceId"] == NS


def test_a_stale_verify_loses_the_condition_and_writes_nothing(store):
    _seed(store, lastScanJobId="SCAN#2026-02-01T00:00:00Z")
    item = store["coa-dev-sources"][(SRC["PK"], SRC["SK"])]
    gv._approve_holding_claim(item, NS, CLAIM, JOB_SK)
    assert store["coa-dev-sources"][(SRC["PK"], SRC["SK"])]["status"] == "REGISTERED"
    assert (CLAIM["PK"], CLAIM["SK"]) not in store["coa-dev-sources"]


def test_scan_job_is_closed_once(store):
    _seed(store)
    gv._finish_scan_job("s1", JOB_SK, status="COMPLETED", error=None, only_if_open=True)
    row = store["coa-dev-source-scan-jobs"][("SRC#s1", JOB_SK)]
    assert row["status"] == "COMPLETED"
    gv._finish_scan_job("s1", JOB_SK, status="FAILED", error="late", only_if_open=True)
    assert store["coa-dev-source-scan-jobs"][("SRC#s1", JOB_SK)]["status"] == "COMPLETED"
