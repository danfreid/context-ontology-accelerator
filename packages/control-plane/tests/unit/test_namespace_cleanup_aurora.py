# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Backend selection for the GraphRAG index cleanup (Aurora scopes vs AOSS indexes)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.constants import graphrag_index_names

NS = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def aurora_index(monkeypatch):
    """Select Aurora and replace AuroraSearchIndex / the Data API client with mocks."""
    from coa_control_plane.namespace import cleanup

    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
    monkeypatch.setenv("AURORA_CLUSTER_ARN", "arn:aws:rds:us-east-1:123456789012:cluster:c")
    monkeypatch.setenv("AURORA_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:s")
    index = MagicMock()
    index.delete_scope.return_value = 1
    monkeypatch.setattr(cleanup, "AuroraDataApiClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cleanup, "AuroraSearchIndex", lambda *a, **k: index)
    aoss = MagicMock()
    monkeypatch.setattr(cleanup, "AossVectorClient", aoss)
    return index, aoss


@pytest.mark.unit
class TestDeleteGraphragIndexesAurora:
    def test_deletes_every_graphrag_scope_and_skips_aoss(self, aurora_index, monkeypatch):
        from coa_control_plane.namespace import cleanup

        index, aoss = aurora_index
        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")

        removed = cleanup.delete_graphrag_indexes(NS)

        expected = graphrag_index_names(NS)
        assert [c.args[0] for c in index.delete_scope.call_args_list] == expected
        assert removed == expected
        aoss.assert_not_called()

    def test_empty_scopes_are_not_reported_and_work_is_idempotent(self, aurora_index):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        index.delete_scope.return_value = 0
        assert cleanup.delete_graphrag_indexes(NS) == []

    def test_runs_without_an_aoss_endpoint(self, aurora_index, monkeypatch):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        monkeypatch.delenv("OSS_ENDPOINT", raising=False)
        cleanup.delete_graphrag_indexes(NS)
        assert index.delete_scope.call_count == len(graphrag_index_names(NS))

    def test_failure_propagates(self, aurora_index):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        index.delete_scope.side_effect = RuntimeError("aurora unavailable")
        with pytest.raises(RuntimeError, match="aurora unavailable"):
            cleanup.delete_graphrag_indexes(NS)


@pytest.mark.unit
def test_default_backend_still_uses_aoss(monkeypatch):
    from coa_control_plane.namespace import cleanup

    monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
    client = MagicMock()
    client.delete_index.return_value = True
    monkeypatch.setattr(cleanup, "AossVectorClient", lambda **_k: client)
    aurora = MagicMock()
    monkeypatch.setattr(cleanup, "AuroraSearchIndex", aurora)

    assert cleanup.delete_graphrag_indexes(NS) == graphrag_index_names(NS)
    aurora.assert_not_called()
