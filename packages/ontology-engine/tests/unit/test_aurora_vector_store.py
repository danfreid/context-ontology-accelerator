# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AuroraVectorStore — the VectorStore adapter over AuroraSearchIndex.

The shared ``AuroraSearchIndex`` and Data API client are replaced with mocks, so
these tests assert only that the store delegates correctly. No AWS access.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.aurora_search import SearchDoc, SearchHit
from coa_ontology.stores import factory
from coa_ontology.stores.aurora_vector import AuroraVectorStore
from coa_ontology.stores.base import EmbeddingHit

pytestmark = pytest.mark.unit

SCOPE = "ontology-workbench-embeddings-ns1"


def _store() -> tuple[AuroraVectorStore, MagicMock, MagicMock]:
    store = AuroraVectorStore(namespace="ns1")
    index = MagicMock()
    client = MagicMock()
    store._index = index
    store._client = client
    return store, index, client


def _item(**over):
    item = {
        "entity_uri": "http://x/A",
        "ontology_id": "onto1",
        "entity_type": "class",
        "embedding_type": "lexical",
        "model_id": "titan",
        "vector": [0.1, 0.2],
        "text": "Alpha",
    }
    item.update(over)
    return item


class TestWrites:
    def test_store_embedding_upserts_with_padding_and_metadata(self):
        store, index, _ = _store()
        out = store.store_embedding(_item(context_text="ctx", data_source_id="ds1", query_backend="RDF_SPARQL"))
        index.ensure_vector_index.assert_called_once()
        scope, docs = index.upsert_docs.call_args.args
        assert scope == SCOPE
        doc: SearchDoc = docs[0]
        assert doc.doc_kind == "ontology"
        assert doc.body == "Alpha"
        assert len(doc.embedding) == store.dimensions
        assert doc.embedding[:2] == [0.1, 0.2]
        assert doc.entity_uri == "http://x/A"
        assert doc.ontology_id == "onto1"
        assert doc.metadata["entity_type"] == "class"
        assert doc.metadata["data_source_id"] == "ds1"
        assert doc.metadata["query_backend"] == "RDF_SPARQL"
        assert doc.metadata["context_text"] == "ctx"
        assert doc.label is None
        assert out["embedding"] == doc.embedding
        assert out["context_text"] == "ctx"

    def test_label_and_aliases_pass_through_only_when_present(self):
        store, index, _ = _store()
        store.store_embedding(_item(label="Alpha Corp", aliases=["Alpha", "AC"]))
        doc = index.upsert_docs.call_args.args[1][0]
        assert doc.label == "Alpha Corp"
        assert list(doc.aliases) == ["Alpha", "AC"]

    def test_batch_groups_by_namespace(self):
        store, index, _ = _store()
        out = store.store_embeddings_batch([_item(), _item(entity_uri="http://x/B", namespace="other")])
        assert len(out) == 2
        scopes = sorted(c.args[0] for c in index.upsert_docs.call_args_list)
        assert scopes == [SCOPE, "ontology-workbench-embeddings-other"]

    def test_empty_batch_is_a_no_op(self):
        store, index, _ = _store()
        assert store.store_embeddings_batch([]) == []
        index.upsert_docs.assert_not_called()


class TestReads:
    def test_reads_never_ensure_the_hnsw_index(self):
        store, index, client = _store()
        client.execute.return_value = []
        index.vector_search.return_value = []
        store.get_embeddings_for_entity("http://x/A")
        store.list_embeddings_for_ontology("onto1")
        store.list_searchable_entity_uris("onto1")
        store.search_nearest([0.1], "lexical", "titan")
        index.ensure_vector_index.assert_not_called()
        index.upsert_docs.assert_not_called()

    def test_get_embeddings_for_entity_selects_by_containment_and_parses_vector(self):
        store, _, client = _store()
        client.execute.return_value = [
            {"doc_id": "d1", "body": "Alpha", "metadata": {"entity_uri": "http://x/A"}, "embedding": "[0.5,0.25]"}
        ]
        docs = store.get_embeddings_for_entity("http://x/A", entity_type="class", embedding_type="lexical")
        sql, params = client.execute.call_args.args
        assert "metadata @> CAST(:m AS jsonb)" in sql
        assert params["ns"] == SCOPE
        assert params["m"] == {"entity_uri": "http://x/A", "entity_type": "class", "embedding_type": "lexical"}
        assert docs == [{"entity_uri": "http://x/A", "text": "Alpha", "embedding": [0.5, 0.25]}]

    def test_list_embeddings_limit_excludes_vector(self):
        store, _, client = _store()
        client.execute.return_value = [{"doc_id": "d1", "body": "t", "metadata": '{"entity_uri": "u"}'}]
        docs = store.list_embeddings_for_ontology("onto1", limit=5)
        sql, params = client.execute.call_args.args
        assert "CAST(embedding AS text)" not in sql
        assert params["lim"] == 5
        assert docs == [{"entity_uri": "u", "text": "t"}]

    def test_list_embeddings_without_limit_includes_vector(self):
        store, _, client = _store()
        client.execute.return_value = []
        store.list_embeddings_for_ontology("onto1")
        sql, params = client.execute.call_args.args
        assert "CAST(embedding AS text)" in sql
        assert params["m"] == {"ontology_id": "onto1"}

    def test_list_embeddings_limit_below_one_returns_empty_without_query(self):
        store, _, client = _store()
        assert store.list_embeddings_for_ontology("onto1", limit=0) == []
        client.execute.assert_not_called()

    def test_list_searchable_entity_uris(self):
        store, _, client = _store()
        client.execute.return_value = [{"entity_uri": "u1"}, {"entity_uri": None}, {"entity_uri": "u2"}]
        assert store.list_searchable_entity_uris("onto1") == ["u1", "u2"]

    def test_per_call_namespace_override(self):
        store, _, client = _store()
        client.execute.return_value = []
        store.list_embeddings_for_ontology("onto1", namespace="other")
        assert client.execute.call_args.args[1]["ns"] == "ontology-workbench-embeddings-other"


class TestSearchNearest:
    def test_score_is_raw_cosine_and_filters_are_mapped(self):
        store, index, _ = _store()
        index.vector_search.return_value = [
            SearchHit(
                key="http://x/A",
                doc_id="d1",
                score=-0.25,
                doc_kind="ontology",
                body="Alpha",
                metadata={
                    "entity_uri": "http://x/A",
                    "embedding_type": "lexical",
                    "model_id": "titan",
                    "ontology_id": "onto1",
                    "entity_type": "class",
                },
            )
        ]
        hits = store.search_nearest([0.1, 0.2], "lexical", "titan", entity_type="class", ontology_id="onto1", top_k=3)
        scope, vector, top_k, filters = index.vector_search.call_args.args
        assert (scope, vector, top_k) == (SCOPE, [0.1, 0.2], 3)
        assert filters == {
            "doc_kind": "ontology",
            "embedding_type": "lexical",
            "model_id": "titan",
            "entity_type": "class",
            "ontology_id": "onto1",
        }
        assert hits == [
            EmbeddingHit(
                entity_uri="http://x/A",
                embedding_type="lexical",
                model_id="titan",
                vector=[],
                ontology_id="onto1",
                entity_type="class",
                score=-0.25,
                text="Alpha",
            )
        ]


class TestDeletes:
    def test_delete_for_ontology_scoped_by_ontology_id(self):
        store, index, _ = _store()
        index.delete_docs.return_value = 4
        assert store.delete_embeddings_for_ontology("onto1") == 4
        index.delete_docs.assert_called_once_with(SCOPE, filter_metadata={"ontology_id": "onto1"})

    def test_delete_for_entities_always_includes_ontology_id(self):
        store, index, _ = _store()
        index.delete_docs.return_value = 1
        assert store.delete_embeddings_for_entities(["u1", "u1", "", "u2"], "onto1") == 2
        calls = [c.kwargs["filter_metadata"] for c in index.delete_docs.call_args_list]
        assert calls == [
            {"ontology_id": "onto1", "entity_uri": "u1"},
            {"ontology_id": "onto1", "entity_uri": "u2"},
        ]

    def test_delete_for_entities_requires_ontology_id_and_uris(self):
        store, index, _ = _store()
        assert store.delete_embeddings_for_entities(["u1"], "") == 0
        assert store.delete_embeddings_for_entities([], "onto1") == 0
        index.delete_docs.assert_not_called()

    def test_delete_index_clears_scope(self):
        store, index, _ = _store()
        index.delete_scope.return_value = 3
        assert store.delete_index() is True
        index.delete_scope.assert_called_once_with(SCOPE)
        index.delete_scope.return_value = 0
        assert store.delete_index(namespace="other") is False


class TestHealth:
    def test_ok(self):
        store, _, client = _store()
        assert store.health_check() == {"status": "ok"}
        assert client.execute.call_args.args[0] == "SELECT 1 AS ok"

    def test_failure_does_not_raise(self):
        store, _, client = _store()
        client.execute.side_effect = RuntimeError("boom")
        assert store.health_check()["status"] == "error"


class TestFactory:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        factory._store_cache.clear()
        yield
        factory._store_cache.clear()

    def test_aurora_pgvector_returns_aurora_store(self, monkeypatch):
        monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
        _graph, vector = factory.build_stores("ns-aurora")
        assert isinstance(vector, AuroraVectorStore)

    def test_default_is_opensearch(self, monkeypatch):
        from coa_ontology.stores.opensearch_vector import OpenSearchVectorStore

        monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
        _graph, vector = factory.build_stores("ns-default")
        assert isinstance(vector, OpenSearchVectorStore)

    def test_unknown_backend_still_raises(self, monkeypatch):
        monkeypatch.setenv("WORKBENCH_BACKEND", "nope")
        with pytest.raises(ValueError, match="aurora_pgvector"):
            factory.build_stores("ns-bad")
