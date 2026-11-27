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
    client = MagicMock()
    client.execute.return_value = [{"n": 0}]
    monkeypatch.setattr(cleanup, "build_client_from_env", lambda *a, **k: client)
    monkeypatch.setattr(cleanup, "AuroraSearchIndex", lambda *a, **k: index)
    aoss = MagicMock()
    monkeypatch.setattr(cleanup, "AossVectorClient", aoss)
    index.client = client
    return index, aoss


@pytest.mark.unit
class TestDeleteGraphragIndexesAurora:
    def test_deletes_every_graphrag_scope_and_still_deletes_aoss_indexes(self, aurora_index, monkeypatch):
        from coa_control_plane.namespace import cleanup

        index, aoss = aurora_index
        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
        aoss.return_value.delete_index.return_value = True
        removed = cleanup.delete_graphrag_indexes(NS)
        expected = graphrag_index_names(NS)
        assert [c.args[0] for c in index.delete_scope.call_args_list] == expected
        assert [c.args[0] for c in aoss.return_value.delete_index.call_args_list] == expected
        assert removed == expected

    def test_aoss_failure_propagates_after_aurora_clear(self, aurora_index, monkeypatch):
        from coa_control_plane.namespace import cleanup

        index, aoss = aurora_index
        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
        aoss.return_value.delete_index.side_effect = RuntimeError("aoss unavailable")
        with pytest.raises(RuntimeError, match="aoss unavailable"):
            cleanup.delete_graphrag_indexes(NS)
        assert index.delete_scope.call_count == len(graphrag_index_names(NS))

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
class TestDeleteAuroraNamespaceRows:
    def test_aurora_branch_deletes_triples_and_ontology_rows(self, aurora_index, monkeypatch):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        monkeypatch.delenv("OSS_ENDPOINT", raising=False)
        cleanup.delete_graphrag_indexes(NS)
        sqls = [c.args[0] for c in index.client.execute.call_args_list]
        assert any("DELETE FROM coa_triple" in s and "namespace = :ns" in s for s in sqls)
        assert any("DELETE FROM ontology_engine" in s and "pk LIKE :prefix" in s for s in sqls)

    def test_batches_until_a_short_batch(self, aurora_index):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        full = cleanup._AURORA_DELETE_BATCH
        index.client.execute.side_effect = [[{"n": full}], [{"n": 7}], [{"n": 0}]]
        assert cleanup._delete_aurora_namespace_rows(NS) == {"coa_triple": full + 7, "ontology_engine": 0}
        calls = index.client.execute.call_args_list
        assert len(calls) == 3
        assert all(c.args[1]["batch"] == full for c in calls)
        assert calls[0].args[1]["ns"] == NS
        assert calls[2].args[1] == {"prefix": f"{NS}#%", "ns_pk": f"NAMESPACE#{NS}", "batch": full}

    def test_like_prefix_escapes_wildcards(self):
        from coa_control_plane.namespace import cleanup

        assert cleanup._like_escape("a_b%c\\d") == "a\\_b\\%c\\\\d"

    def test_failure_propagates(self, aurora_index):
        from coa_control_plane.namespace import cleanup

        index, _ = aurora_index
        index.client.execute.side_effect = RuntimeError("aurora unavailable")
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
