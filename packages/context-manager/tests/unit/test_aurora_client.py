# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AuroraVectorClient with a mocked AuroraSearchIndex (no AWS)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.aurora_search import SearchHit, SearchMode
from coa_common.constants import StructuredBackend
from coa_serve.clients.aurora import AuroraVectorClient
from coa_serve.clients.base import VectorClient

pytestmark = pytest.mark.unit


def _client() -> tuple[AuroraVectorClient, MagicMock]:
    index = MagicMock()
    return AuroraVectorClient(index="chunk_ns", search_index=index), index


def _hit(score: float = 0.5, **meta) -> SearchHit:
    return SearchHit(
        key="k",
        doc_id="doc-1",
        score=score,
        body="Alpha text",
        metadata={"entity_uri": "http://x/A", "entity_type": "class", "data_source_id": "ds1", **meta},
    )


def test_concurrent_first_use_builds_the_index_once(monkeypatch):
    import threading
    import time

    from coa_serve.clients import aurora

    built = []
    monkeypatch.setattr(aurora, "build_client_from_env", lambda *a, **k: MagicMock(config=MagicMock(dimensions=8)))

    def slow_index(client, dimensions):
        time.sleep(0.05)
        built.append(dimensions)
        return MagicMock()

    monkeypatch.setattr(aurora, "AuroraSearchIndex", slow_index)
    client = AuroraVectorClient()
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(client._aurora())) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert built == [8]
    assert len({id(i) for i in seen}) == 1


def test_is_vector_client():
    assert isinstance(AuroraVectorClient(search_index=MagicMock()), VectorClient)


class TestSearch:
    async def test_filters_are_mapped_and_score_converted(self):
        client, index = _client()
        index.vector_search.return_value = [_hit(score=0.5), _hit(score=-1.0), _hit(score=1.0)]
        hits = await client.search(
            [0.1, 0.2],
            top_k=7,
            entity_type="class",
            require_mapped=True,
            ontology_ids=["o1", "o2"],
        )
        scope, vector, top_k, filters = index.vector_search.call_args.args
        assert (scope, vector, top_k) == ("chunk_ns", [0.1, 0.2], 7)
        assert filters == {"entity_type": "class", "require_mapped": True, "ontology_ids": ["o1", "o2"]}
        assert [h.score for h in hits] == [0.75, 0.0, 1.0]
        assert hits[0].id == "doc-1"
        assert hits[0].text == "Alpha text"
        assert hits[0].metadata["entity_uri"] == "http://x/A"
        assert hits[0].data_source_id == "ds1"

    async def test_index_argument_overrides_default_scope(self):
        client, index = _client()
        index.vector_search.return_value = []
        await client.search([0.1], index="other")
        assert index.vector_search.call_args.args[0] == "other"
        assert index.vector_search.call_args.args[3] is None

    async def test_graph_backend_requires_query_backend(self):
        client, index = _client()
        index.vector_search.return_value = []
        await client.search([0.1], query_backend=StructuredBackend.RDF_SPARQL)
        assert index.vector_search.call_args.args[3] == {"query_backend": "RDF_SPARQL"}

    async def test_sql_backend_excludes_graph_documents(self):
        client, index = _client()
        index.vector_search.return_value = []
        await client.search([0.1], query_backend=StructuredBackend.SQL)
        assert index.vector_search.call_args.args[3] == {"exclude_metadata": {"query_backend": "RDF_SPARQL"}}

    async def test_chunk_identity_comes_from_nested_metadata(self):
        client, index = _client()
        index.vector_search.return_value = [
            SearchHit(
                key="k",
                doc_id="d",
                score=0.0,
                body="chunk",
                metadata={"metadata": {"source": {"sourceId": "src-1", "metadata": {"source_s3_key": "a/b/c.pdf"}}}},
            )
        ]
        hits = await client.search([0.1])
        assert hits[0].metadata["source_doc"] == "src-1"
        assert hits[0].metadata["source_doc_name"] == "c.pdf"


class TestSearchHybrid:
    async def test_delegates_with_mode_and_keeps_fused_score(self):
        client, index = _client()
        index.search.return_value = [_hit(score=0.03)]
        hits = await client.search_hybrid("alpha", [0.1], top_k=4, mode=SearchMode.LEXICAL, ontology_ids=["o1"])
        args = index.search.call_args.args
        assert args == ("chunk_ns", "alpha", [0.1], SearchMode.LEXICAL, 4, {"ontology_ids": ["o1"]})
        assert hits[0].score == 0.03


class TestPhraseSearch:
    async def test_shape_and_matched_indexes(self):
        client, index = _client()
        hit = SearchHit(
            key="k",
            doc_id="doc-9",
            score=1.5,
            body="Glencore plc signed",
            metadata={
                "chunk": {"chunkId": "chunk-9"},
                "source": {"sourceId": "src-1", "metadata": {"source_s3_key": "docs/a.pdf"}},
            },
            matched_phrases=["Glencore plc"],
        )
        index.keyword_search.return_value = [hit]
        out = await client.phrase_search(["Glencore  plc", "", "  ", "Rio Tinto"], index="chunk_ns", size=20)
        args = index.keyword_search.call_args
        assert args.args == ("chunk_ns", "", 20)
        assert args.kwargs == {"phrases": ["Glencore plc", "Rio Tinto"]}
        assert out == [
            {
                "chunk_id": "chunk-9",
                "source_id": "src-1",
                "source_s3_key": "docs/a.pdf",
                "text": "Glencore plc signed",
                "score": 1.5,
                "matched": [0],
            }
        ]

    async def test_chunk_id_falls_back_to_doc_id(self):
        client, index = _client()
        index.keyword_search.return_value = [SearchHit(key="k", doc_id="doc-2", score=1.0, body="t")]
        out = await client.phrase_search(["t"], index="chunk_ns")
        assert out[0]["chunk_id"] == "doc-2"
        assert out[0]["matched"] == []
        assert out[0]["source_id"] == ""

    async def test_blank_phrases_skipped(self):
        client, index = _client()
        assert await client.phrase_search(["", "   "], index="chunk_ns") == []
        assert await client.phrase_search([], index="chunk_ns") == []
        index.keyword_search.assert_not_called()


class TestHealth:
    async def test_healthy(self):
        client, index = _client()
        result = await client.health_check()
        assert result["status"] == "healthy"
        index._client.execute.assert_called_once()

    async def test_unhealthy_does_not_raise(self):
        client, index = _client()
        index._client.execute.side_effect = RuntimeError("boom")
        assert await client.health_check() == {"status": "unhealthy", "error": "connectivity check failed"}
