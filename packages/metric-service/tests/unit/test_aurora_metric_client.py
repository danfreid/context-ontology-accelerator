# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Aurora branch of MetricOpenSearchClient (Aurora index mocked, no AWS)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from coa_common.aurora_search import SearchHit
from coa_common.constants import URN_PREFIX
from coa_metrics.neptune_client import MetricDefinition, MetricDialect
from coa_metrics.opensearch_client import MetricOpenSearchClient

pytestmark = pytest.mark.unit

SCOPE = "ontology-workbench-embeddings-test-ns"
ENTITY_URI = f"urn:{URN_PREFIX}:test-ns:metric:order_count"


@pytest.fixture
def metric() -> MetricDefinition:
    return MetricDefinition(
        name="order_count",
        description="Number of orders",
        expression_dialects=[MetricDialect(dialect="trino", expression="COUNT(*)")],
        data_source_id="ds-abc123",
        source_table="orders",
    )


def _aurora_client() -> tuple[MetricOpenSearchClient, MagicMock, MagicMock]:
    client = MetricOpenSearchClient()
    index = MagicMock()
    aoss = MagicMock()
    embedder = MagicMock()
    embedder.embed_document.return_value = [0.1, 0.2]
    client._aurora_index = index
    client._client = aoss
    client._embedder = embedder
    return client, index, aoss


def test_aurora_index_dimension_comes_from_aurora_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from coa_metrics import opensearch_client as mod

    cfg = MagicMock(cluster_arn="c", secret_arn="s", database="d", dimensions=512)
    monkeypatch.setattr(mod.AuroraConfig, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(mod, "AuroraDataApiClient", lambda *a, **k: MagicMock())
    built = MagicMock()
    monkeypatch.setattr(mod, "AuroraSearchIndex", built)
    MetricOpenSearchClient()._aurora()
    assert built.call_args.args[1] == 512


def test_concurrent_first_calls_build_one_aurora_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from coa_metrics import opensearch_client as mod

    cfg = MagicMock(cluster_arn="c", secret_arn="s", database="d", dimensions=512)
    monkeypatch.setattr(mod.AuroraConfig, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(mod, "AuroraDataApiClient", lambda *a, **k: MagicMock())
    built: list[object] = []

    def slow_build(*_a: object, **_k: object) -> object:
        time.sleep(0.05)  # widen the race window
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr(mod, "AuroraSearchIndex", slow_build)
    client = MetricOpenSearchClient()
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(client._aurora())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert all(r is built[0] for r in results)


def test_default_environment_uses_aoss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    assert MetricOpenSearchClient()._use_aurora() is False


def test_pgvector_environment_uses_aurora(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
    assert MetricOpenSearchClient()._use_aurora() is True


def test_embed_metric_upserts_into_aurora(monkeypatch: pytest.MonkeyPatch, metric: MetricDefinition) -> None:
    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
    client, index, aoss = _aurora_client()
    client.embed_metric("test-ns", metric)
    index.ensure_vector_index.assert_called_once()
    scope, docs = index.upsert_docs.call_args.args
    assert scope == SCOPE
    doc = docs[0]
    assert doc.doc_kind == "metric"
    assert doc.entity_uri == ENTITY_URI
    assert doc.embedding == [0.1, 0.2]
    assert doc.metadata == {"entity_type": "metric", "namespace": "test-ns"}
    assert doc.label == "order_count"
    aoss.index_document.assert_not_called()
    aoss.delete_by_term.assert_not_called()


def test_delete_metric_embedding_on_aurora(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
    client, index, aoss = _aurora_client()
    client.delete_metric_embedding("test-ns", "order_count")
    index.delete_docs.assert_called_once_with(
        SCOPE, filter_metadata={"entity_uri": ENTITY_URI, "entity_type": "metric"}
    )
    aoss.delete_by_term.assert_not_called()


def test_search_metrics_on_aurora_maps_hits_and_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
    client, index, aoss = _aurora_client()
    index.vector_search.return_value = [
        SearchHit(
            key=ENTITY_URI,
            doc_id="d1",
            score=0.5,
            doc_kind="metric",
            body="order_count Number of orders",
            metadata={"entity_uri": ENTITY_URI, "namespace": "test-ns"},
        )
    ]
    results = client.search_metrics("test-ns", [0.1, 0.2], top_k=3)
    assert index.vector_search.call_args.args == (
        SCOPE,
        [0.1, 0.2],
        3,
        {"doc_kind": "metric", "entity_type": "metric", "namespace": "test-ns"},
    )
    assert results == [
        {
            "entity_uri": ENTITY_URI,
            "metric_name": "order_count",
            "namespace": "test-ns",
            "text": "order_count Number of orders",
            "score": 0.75,
        }
    ]
    aoss.knn_search.assert_not_called()


def test_aoss_path_unchanged_by_default(monkeypatch: pytest.MonkeyPatch, metric: MetricDefinition) -> None:
    monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    client, index, aoss = _aurora_client()
    aoss.ensure_index.side_effect = lambda idx: idx
    client.embed_metric("test-ns", metric)
    aoss.index_document.assert_called_once()
    index.upsert_docs.assert_not_called()
