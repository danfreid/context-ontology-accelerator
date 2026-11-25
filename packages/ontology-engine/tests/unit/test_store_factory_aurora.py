# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""WORKBENCH_BACKEND selection in stores.factory, including aurora_pgvector."""

from __future__ import annotations

import pytest
from coa_ontology.stores import factory
from coa_ontology.stores.aurora.graph import AuroraGraphStore
from coa_ontology.stores.aurora.vector import AuroraVectorStore
from coa_ontology.stores.base import GraphStore, VectorStore

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache():
    factory._store_cache.clear()
    yield
    factory._store_cache.clear()


def test_aurora_pgvector_builds_the_aurora_pair(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
    graph, vector = factory.build_stores("ns1")
    assert isinstance(graph, AuroraGraphStore)
    assert isinstance(vector, AuroraVectorStore)
    assert isinstance(graph, GraphStore) and isinstance(vector, VectorStore)
    assert graph._namespace == "ns1" and vector._namespace == "ns1"


def test_aurora_pgvector_value_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BACKEND", "  Aurora_PgVector ")
    graph, _ = factory.build_stores()
    assert isinstance(graph, AuroraGraphStore)


def test_pair_is_cached_per_namespace(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
    assert factory.build_stores("a") is factory.build_stores("a")
    assert factory.build_stores("a") is not factory.build_stores("b")


def test_construction_needs_no_aurora_settings(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
    for k in ("AURORA_CLUSTER_ARN", "AURORA_RESOURCE_ARN", "AURORA_CLUSTER_ENDPOINT", "AURORA_SECRET_ARN"):
        monkeypatch.delenv(k, raising=False)
    graph, vector = factory.build_stores("x")
    assert graph._client is None and vector._client is None


@pytest.mark.parametrize("backend", ["na_only", "opensearch_neptune"])
def test_existing_backends_still_build_their_own_pairs(monkeypatch, backend):
    class FakeGraph:
        def __init__(self, namespace=None):
            self.namespace = namespace

    class FakeVector(FakeGraph):
        pass

    if backend == "na_only":
        monkeypatch.setattr("coa_ontology.stores.na_graph.NeptuneAnalyticsGraphStore", FakeGraph)
        monkeypatch.setattr("coa_ontology.stores.na_vector.NeptuneAnalyticsVectorStore", FakeVector)
    else:
        monkeypatch.setattr("coa_ontology.stores.neptune_db_graph.NeptuneDBGraphStore", FakeGraph)
        monkeypatch.setattr("coa_ontology.stores.opensearch_vector.OpenSearchVectorStore", FakeVector)
    monkeypatch.setenv("WORKBENCH_BACKEND", backend)
    graph, vector = factory.build_stores("n")
    assert type(graph) is FakeGraph and type(vector) is FakeVector


def test_default_backend_is_unchanged(monkeypatch):
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    assert factory._selected_backend() == "opensearch_neptune"
    assert factory.DEFAULT_BACKEND == "opensearch_neptune"


def test_unknown_backend_still_raises_and_names_all_three(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BACKEND", "mongodb")
    with pytest.raises(ValueError, match="mongodb") as err:
        factory.build_stores("n")
    for name in ("opensearch_neptune", "na_only", "aurora_pgvector"):
        assert name in str(err.value)
    assert "n" not in factory._store_cache
