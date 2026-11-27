# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Import-job offset claims with ``METADATA_BACKEND=aurora`` over the in-memory Aurora metadata store."""

from __future__ import annotations

import pytest
from coa_common.testing_aurora import install
from coa_metrics.api import import_job_store as store_mod
from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset, create_job, get_job

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def aurora(monkeypatch):
    monkeypatch.setattr(store_mod, "_dynamodb", None)
    monkeypatch.setattr(store_mod, "_TABLE_NAME", "coa-dev-metric-import-jobs")
    yield install(monkeypatch)
    monkeypatch.setattr(store_mod, "_dynamodb", None)


def test_create_get_and_claim_race():
    job = create_job("ns-1", "imports/a.csv", 100, chunk_size=50)
    job_id = job["SK"].removeprefix("IMPORT#")
    assert get_job("ns-1", job_id, consistent_read=True)["metricsTotal"] == 100

    first = claim_job_offset("ns-1", job_id, offset=0, end_offset=50, chunk_size=50, lease_seconds=60)
    assert first.state == OffsetClaimState.ACQUIRED
    assert first.token

    second = claim_job_offset("ns-1", job_id, offset=0, end_offset=50, chunk_size=50, lease_seconds=60)
    assert second.state == OffsetClaimState.LEASE_HELD  # the conditional update lost to the live lease


def test_claim_on_a_missing_job_is_not_active():
    claim = claim_job_offset("ns-1", "missing", offset=0, end_offset=50, chunk_size=50, lease_seconds=60)
    assert claim.state == OffsetClaimState.NOT_ACTIVE
