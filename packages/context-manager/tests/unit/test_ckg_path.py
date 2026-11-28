# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora document graph + S3 Vectors path: retriever factory branch and topic_beam seeding."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.unit

_NEPTUNE = "g-abc1234567.us-east-1.neptune-graph.amazonaws.com"
_AOSS = "https://test.aoss.amazonaws.com"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GRAPH_BACKEND", "GRAPHRAG_VECTOR_BACKEND", "GRAPHRAG_S3_VECTOR_BUCKET", "GRAPHRAG_S3_VECTOR_PREFIX"):
        monkeypatch.delenv(name, raising=False)


def _config(neptune: str = _NEPTUNE, aoss: str = _AOSS) -> MagicMock:
    cfg = MagicMock()
    cfg.tier3_strategy = "lexical-baseline"
    cfg.neptune_endpoint = neptune
    cfg.opensearch_endpoint = aoss
    return cfg


class TestRetrieverFactoryBranch:
    def test_default_flags_keep_neptune_and_aoss_uris(self):
        from coa_serve.clients.factory import build_lexical_retriever

        r = build_lexical_retriever(_config())
        assert r is not None
        assert r._graph_store_uri == "neptune-graph://g-abc1234567"
        assert r._vector_store_uri == f"aoss://{_AOSS}"

    def test_default_flags_still_require_both_endpoints(self):
        from coa_serve.clients.factory import build_lexical_retriever

        assert build_lexical_retriever(_config(neptune="")) is None
        assert build_lexical_retriever(_config(aoss="")) is None

    def test_graph_backend_alone_does_not_switch(self, monkeypatch):
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        assert build_lexical_retriever(_config(neptune="", aoss="")) is None

    def test_ckg_path_uses_aurora_and_s3_vectors_without_endpoints(self, monkeypatch):
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", "s3vectors")
        monkeypatch.setenv("GRAPHRAG_S3_VECTOR_BUCKET", "vb")
        r = build_lexical_retriever(_config(neptune="", aoss=""))
        assert r is not None
        assert r._graph_store_uri == "aurora-pg://?schema=lg"
        assert r._vector_store_uri == "s3vectors://vb/graphrag"

    def test_ckg_path_registers_the_aurora_graph_store(self, monkeypatch):
        from coa_serve.clients.factory import build_lexical_retriever
        from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", "s3vectors")
        monkeypatch.setenv("GRAPHRAG_S3_VECTOR_BUCKET", "vb")
        build_lexical_retriever(_config(neptune="", aoss=""))
        store = GraphStoreFactory.for_graph_store("aurora-pg://?schema=lg")
        assert type(store).__name__ == "AuroraGraphStore"


class _NoQueryClient:
    """A topic-index client whose ``query`` must never be called (S3 Vectors has none)."""

    def query(self, *_a, **_k):
        raise AssertionError("topic_beam must not call topic_index.client.query on the s3vectors path")


class _FakeTopicIndex:
    def __init__(self, hits):
        self.client = _NoQueryClient()
        self.hits = hits
        self.calls: list[int] = []
        self.embed_model = MagicMock()
        self.embed_model.get_query_embedding.return_value = [0.1, 0.2]

    def top_k(self, query_bundle, top_k=5, filter_config=None):
        self.calls.append(top_k)
        return self.hits


def _beam(index: _FakeTopicIndex):
    from coa_serve.lexical.s3vectors_topic_beam import S3VectorsTopicBeamSearch

    beam = S3VectorsTopicBeamSearch.__new__(S3VectorsTopicBeamSearch)
    beam.top_k = 4
    beam._topic_cache = {"stale": np.array([1.0])}
    beam._query_embedding = None
    beam.vector_store = MagicMock()
    beam.vector_store.get_index.return_value = index
    return beam


class TestS3VectorsTopicBeamSearch:
    def test_seeds_with_top_k_and_never_client_query(self):
        from llama_index.core import QueryBundle

        index = _FakeTopicIndex(
            [{"topic": {"topicId": "t1"}}, {"topic": {}}, {"topic": {"topicId": "t2"}}, {"other": 1}]
        )
        beam = _beam(index)
        ids = beam.get_start_node_ids(QueryBundle(query_str="who pays?", embedding=[0.1, 0.2]))
        assert ids == ["t1", "t2"]
        assert index.calls == [4]
        beam.vector_store.get_index.assert_called_with("topic")
        assert beam._topic_cache == {}
        assert list(beam._query_embedding) == [0.1, 0.2]

    def test_is_a_topic_beam_search(self):
        from coa_serve.lexical.s3vectors_topic_beam import S3VectorsTopicBeamSearch
        from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch

        assert issubclass(S3VectorsTopicBeamSearch, TopicBeamSearch)


class TestEngineSelection:
    @staticmethod
    def _build(monkeypatch):
        """Run the real builder with store wrapping and engine construction stubbed; return the retriever class."""
        import coa_serve.lexical.strategies as strategies
        from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
        from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch
        from graphrag_toolkit.lexical_graph.storage.graph.multi_tenant_graph_store import MultiTenantGraphStore
        from graphrag_toolkit.lexical_graph.storage.vector.multi_tenant_vector_store import MultiTenantVectorStore
        from graphrag_toolkit.lexical_graph.storage.vector.read_only_vector_store import ReadOnlyVectorStore

        built: list[type] = []
        for cls in (MultiTenantGraphStore, MultiTenantVectorStore, ReadOnlyVectorStore):
            monkeypatch.setattr(cls, "wrap", classmethod(lambda _c, store, *a, **k: store))

        def _init(self, *a, **k):
            built.append(type(self))

        monkeypatch.setattr(TopicBeamSearch, "__init__", _init)
        monkeypatch.setattr(LexicalGraphQueryEngine, "__init__", lambda self, *a, **k: None)
        strategies._build_topic_beam_engine(MagicMock(), MagicMock(), tenant_id="abc")
        return built[0]

    def test_default_flags_use_stock_topic_beam(self, monkeypatch):
        from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch

        assert self._build(monkeypatch) is TopicBeamSearch

    def test_ckg_path_uses_the_s3_vectors_subclass(self, monkeypatch):
        from coa_serve.lexical.s3vectors_topic_beam import S3VectorsTopicBeamSearch

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", "s3vectors")
        assert self._build(monkeypatch) is S3VectorsTopicBeamSearch
