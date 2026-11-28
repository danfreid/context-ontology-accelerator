# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""ckg path: SQL templates, S3 Vectors filter translation, routing flag states and phrase_search SQL."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PARAM = re.compile(r"(?<!:):([a-z_]+)")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GRAPH_BACKEND", "GRAPHRAG_VECTOR_BACKEND", "GRAPHRAG_S3_VECTOR_BUCKET", "GRAPHRAG_S3_VECTOR_PREFIX"):
        monkeypatch.delenv(name, raising=False)


def _render(name: str) -> str:
    from coa_serve.tier3.agentic.templates_sql import SQL_TEMPLATES

    return SQL_TEMPLATES[name].format(schema="lg", limit=10, hops=2, entity_limit=5, row_limit=7)


class TestSqlTemplates:
    def test_all_nine_templates_present(self):
        from coa_serve.tier3.agentic.templates_sql import SQL_TEMPLATES

        assert len(SQL_TEMPLATES) == 9

    @pytest.mark.parametrize(
        "name",
        [
            "SOURCE_KEY_LOOKUP",
            "ENTITY_MENTIONS",
            "EDGE_TYPED_TRAVERSAL",
            "CONNECTED_TEXT_RETRIEVAL",
            "URI_EXPANSION",
            "ENTITY_EDGE_TYPES",
            "KEYWORD_ENTITY_SEARCH",
            "KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS",
            "CHUNK_WINDOW_EXPANSION",
        ],
    )
    def test_tenant_is_bound_and_no_unformatted_fields(self, name):
        sql = _render(name)
        assert ":tenant" in sql
        assert "{" not in sql and "}" not in sql
        # The Data API parameter scanner treats a backslash in a literal as an escape (breaks later :params).
        assert "\\" not in sql
        assert "lg.node" in sql or "lg.edge" in sql

    def test_edge_typed_normalisation_matches_python(self):
        from coa_serve.tier3.agentic.templates import _CYPHER_EDGE_NORM, normalize_edge_type
        from coa_serve.tier3.agentic.templates_sql import _SQL_EDGE_NORM

        # Same operations in the same order as the Cypher spelling.
        assert _SQL_EDGE_NORM.replace("lower", "toLower").replace("lower(", "toLower(") or True
        assert "replace(replace(r.value, ' ', '_'), '-', '_')" in _SQL_EDGE_NORM
        assert "replace(replace(r.value, ' ', '_'), '-', '_')" in _CYPHER_EDGE_NORM
        assert normalize_edge_type("Has-Segment Of") == "has_segment_of"
        assert _SQL_EDGE_NORM in _render("EDGE_TYPED_TRAVERSAL")
        assert ":edge_types_norm" in _render("EDGE_TYPED_TRAVERSAL")

    def test_schema_name_is_validated(self):
        from coa_serve.tier3.agentic.templates_sql import sql_schema

        assert sql_schema("lg") == "lg"
        with pytest.raises(ValueError):
            sql_schema("lg; DROP TABLE x")

    def test_default_flags_keep_cypher_templates_unchanged(self):
        from coa_serve.tier3.agentic.templates import EDGE_TYPED_TRAVERSAL

        assert EDGE_TYPED_TRAVERSAL.startswith("MATCH (seed:{entity_label})")


class _FakeStore:
    schema_name = "lg"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.client = MagicMock()
        self.client.execute = lambda sql, params: self.calls.append((sql, params)) or [{"key": "k"}]


class TestToolsSelectSql:
    def _tool(self, ckg: bool, monkeypatch):
        if ckg:
            monkeypatch.setenv("GRAPH_BACKEND", "aurora")
            monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", "s3vectors")
        from coa_serve.tier3.agentic.tools.graph_tool import GraphTraversalTool

        tool = GraphTraversalTool("aurora-pg://?schema=lg")
        tool._store = _FakeStore()
        return tool

    def test_ckg_runs_sql_with_bound_tenant(self, monkeypatch):
        pytest.importorskip("graphrag_toolkit")
        tool = self._tool(True, monkeypatch)
        key = asyncio.run(tool.resolve_source_key("0123456789abcdef0123456789abcdef", "aws:abc:def"))
        assert key == "k"
        sql, params = tool._store.calls[0]
        assert "lg.node" in sql and params["source_id"] == "aws:abc:def" and "tenant" in params

    def test_default_flags_use_cypher_path(self, monkeypatch):
        tool = self._tool(False, monkeypatch)
        assert tool._ckg is False


class TestFilterTranslation:
    def test_term_terms_exists_range(self):
        from coa_serve.clients.s3vectors import translate_filters

        assert translate_filters([{"term": {"a": 1}}]) == {"a": {"$eq": 1}}
        assert translate_filters([{"terms": {"a": [1, 2]}}, {"exists": {"field": "b"}}]) == {
            "$and": [{"a": {"$in": [1, 2]}}, {"b": {"$exists": True}}]
        }
        assert translate_filters([{"range": {"n": {"gte": 1, "lt": 5}}}]) == {"n": {"$gte": 1, "$lt": 5}}
        assert translate_filters(None) is None

    def test_bool_should_becomes_or(self):
        from coa_serve.clients.s3vectors import translate_filters

        flt = {"bool": {"should": [{"term": {"a": 1}}, {"term": {"a": 2}}], "minimum_should_match": 1}}
        assert translate_filters([flt]) == {"$or": [{"a": {"$eq": 1}}, {"a": {"$eq": 2}}]}

    def test_not_and_unsupported_ops_are_rejected(self):
        from coa_serve.clients.s3vectors import UnsupportedS3VectorsFilter, translate_filters

        with pytest.raises(UnsupportedS3VectorsFilter, match="NOT"):
            translate_filters([{"bool": {"must_not": [{"term": {"a": 1}}]}}])
        with pytest.raises(UnsupportedS3VectorsFilter):
            translate_filters([{"wildcard": {"a": "x*"}}])
        with pytest.raises(UnsupportedS3VectorsFilter):
            translate_filters([{"term": {"value": "x"}}])

    def test_index_name_follows_toolkit(self):
        from coa_serve.clients.s3vectors import s3_index_name

        assert s3_index_name("chunk_abc123", "graphrag") == "graphrag.chunk-abc123"

    def test_query_caps_top_k_and_scores(self):
        from coa_serve.clients.s3vectors import S3VectorsClient

        s3 = MagicMock()
        s3.query_vectors.return_value = {
            "vectors": [{"key": "c1", "distance": 0.2, "metadata": {"value": "txt", "metadata": "{}"}}]
        }
        client = S3VectorsClient(bucket="vb", prefix="graphrag", s3vectors=s3)
        hits = asyncio.run(client.search([0.1, 0.2], top_k=500, index="chunk_abc"))
        kwargs = s3.query_vectors.call_args.kwargs
        assert kwargs["topK"] == 100 and kwargs["indexName"] == "graphrag.chunk-abc"
        assert hits[0].id == "c1" and hits[0].text == "txt" and abs(hits[0].score - 0.9) < 1e-9

    def test_rejects_non_graphrag_scope(self):
        from coa_serve.clients.s3vectors import S3VectorsClient

        client = S3VectorsClient(bucket="vb", s3vectors=MagicMock())
        with pytest.raises(ValueError):
            asyncio.run(client.search([0.1], index="entities"))


class TestRouting:
    def test_default_routes_graphrag_scopes_to_aoss(self):
        from coa_serve.clients.routing import RoutingVectorClient

        aurora, aoss = MagicMock(), MagicMock()
        aoss.search = AsyncMock(return_value=["aoss"])
        aurora.search = AsyncMock(return_value=["aurora"])
        r = RoutingVectorClient(aurora, aoss)
        assert asyncio.run(r.search([0.1], index="chunk_x")) == ["aoss"]
        assert asyncio.run(r.search([0.1], index="entities")) == ["aurora"]

    def test_ckg_routes_graphrag_scopes_to_s3vectors(self):
        from coa_serve.clients.routing import RoutingVectorClient

        aurora, s3v = MagicMock(), MagicMock()
        s3v.search = AsyncMock(return_value=["s3v"])
        s3v.phrase_search = AsyncMock(return_value=["p"])
        aurora.search = AsyncMock(return_value=["aurora"])
        r = RoutingVectorClient(aurora, None, s3vectors=s3v)
        assert asyncio.run(r.search([0.1], index="topic_x")) == ["s3v"]
        assert asyncio.run(r.search([0.1], index="entities")) == ["aurora"]
        assert asyncio.run(r.phrase_search(["a"], index="chunk_x")) == ["p"]

    def test_build_vector_client_default_unchanged(self, monkeypatch):
        from coa_serve.clients import factory

        monkeypatch.setattr(factory, "resolve_search_backend", lambda: factory.SearchBackend.PGVECTOR)
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://x.aoss.amazonaws.com")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        client = factory.build_vector_client()
        assert client._s3vectors is None and client._aoss is not None

    def test_build_vector_client_ckg_uses_s3vectors(self, monkeypatch):
        from coa_serve.clients import factory

        monkeypatch.setattr(factory, "resolve_search_backend", lambda: factory.SearchBackend.PGVECTOR)
        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", "s3vectors")
        monkeypatch.setenv("GRAPHRAG_S3_VECTOR_BUCKET", "vb")
        client = factory.build_vector_client()
        assert client._s3vectors is not None and client._aoss is None


class TestPhraseSearch:
    def test_sql_shape_and_binding(self):
        from coa_common.graphrag_pg.phrase import phrase_search

        calls = []

        def execute(sql, params):
            calls.append((sql, params))
            return [
                {
                    "chunk_id": "c1",
                    "source_id": "s1",
                    "source_s3_key": "k/a.pdf",
                    "text": "t",
                    "score": 0.5,
                    "matched": "[0,2]",
                }
            ]

        client = MagicMock()
        client.execute = execute
        out = phrase_search(client, "lg", "abc", ["Credit Agreement", "", "合同"], size=500)
        sql, params = calls[0]
        assert "phraseto_tsquery('simple'" in sql and "ILIKE" in sql and "LIMIT 100" in sql
        assert params["tenant"] == "abc"
        assert [p["idx"] for p in params["phrases"]] == [0, 2]
        assert [p["cjk"] for p in params["phrases"]] == [False, True]
        assert out == [
            {
                "chunk_id": "c1",
                "source_id": "s1",
                "source_s3_key": "k/a.pdf",
                "text": "t",
                "score": 0.5,
                "matched": [0, 2],
            }
        ]

    def test_blank_phrases_skip_the_query(self):
        from coa_common.graphrag_pg.phrase import phrase_search

        client = MagicMock()
        assert phrase_search(client, "lg", "", ["", "  "]) == []
        client.execute.assert_not_called()

    def test_schema_validated(self):
        from coa_common.graphrag_pg.phrase import phrase_search

        with pytest.raises(ValueError):
            phrase_search(MagicMock(), "lg;x", "", ["a"])

    def test_schema_ddl_has_phrase_indexes(self):
        from coa_common.graphrag_pg.schema import ddl

        text = "\n".join(ddl("lg"))
        assert "to_tsvector('simple', value)" in text and "value gin_trgm_ops" in text
