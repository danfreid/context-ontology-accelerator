# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for client factory and ClientSet."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.clients.base import (
    ClientSet,
    GraphClient,
    LLMClient,
    QueryExecutor,
    VectorClient,
)


@pytest.mark.unit
class TestProtocolConformance:
    """Verify concrete implementations satisfy the protocol contracts."""

    def test_neptune_is_graph_client(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client: GraphClient = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        assert isinstance(client, GraphClient)

    def test_opensearch_is_vector_client(self):
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client: VectorClient = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")
        assert isinstance(client, VectorClient)

    def test_bedrock_is_llm_client(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        client: LLMClient = BedrockLLMClient()
        assert isinstance(client, LLMClient)

    @patch("coa_serve.clients.source_db.boto3")
    def test_source_db_is_query_executor(self, mock_boto3):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        client: QueryExecutor = SourceDBQueryExecutor(data_sources_table="test-table")
        assert isinstance(client, QueryExecutor)


@pytest.mark.unit
class TestFactory:
    @patch("coa_serve.clients.factory._build_default_clients")
    def test_build_clients_calls_default_builder(self, mock_build):
        from coa_serve.clients.factory import build_clients

        mock_build.return_value = MagicMock(spec=ClientSet)
        result = build_clients()
        mock_build.assert_called_once()
        assert result is mock_build.return_value


@pytest.mark.unit
class TestVectorBackendSelection:
    """_build_default_clients picks the vector client from resolve_search_backend()."""

    @staticmethod
    def _build():
        from coa_serve.clients.factory import _build_default_clients

        with (
            patch("coa_serve.clients.neptune.NeptuneGraphClient"),
            patch("coa_serve.clients.bedrock.BedrockLLMClient"),
            patch("coa_serve.clients.source_db.SourceDBQueryExecutor"),
        ):
            return _build_default_clients()

    def test_default_builds_opensearch_client(self, monkeypatch):
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
        monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "test.aoss.amazonaws.com")
        assert isinstance(self._build().vector, OpenSearchVectorClient)

    def test_pgvector_tsvector_builds_aurora_client(self, monkeypatch):
        from coa_serve.clients.aurora import AuroraVectorClient

        monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
        monkeypatch.delenv("OPENSEARCH_ENDPOINT", raising=False)
        assert isinstance(self._build().vector, AuroraVectorClient)

    def test_pgvector_tsvector_with_opensearch_endpoint_builds_routing_client(self, monkeypatch):
        from coa_serve.clients.routing import RoutingVectorClient

        monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "test.aoss.amazonaws.com")
        assert isinstance(self._build().vector, RoutingVectorClient)


@pytest.mark.unit
class TestBuildVectorClient:
    """build_vector_client is what serve startup (coa_serve.main) uses for the vector client."""

    def test_pgvector_tsvector_builds_aurora_client(self, monkeypatch):
        from coa_serve.clients.aurora import AuroraVectorClient
        from coa_serve.clients.factory import build_vector_client

        monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
        monkeypatch.delenv("OPENSEARCH_ENDPOINT", raising=False)
        assert isinstance(build_vector_client(), AuroraVectorClient)

    @pytest.mark.parametrize("via_arg", [True, False])
    def test_pgvector_tsvector_with_an_endpoint_builds_a_routing_client(self, monkeypatch, via_arg):
        from coa_serve.clients.aurora import AuroraVectorClient
        from coa_serve.clients.factory import build_vector_client
        from coa_serve.clients.opensearch import OpenSearchVectorClient
        from coa_serve.clients.routing import RoutingVectorClient

        monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector+tsvector")
        if via_arg:
            monkeypatch.delenv("OPENSEARCH_ENDPOINT", raising=False)
            client = build_vector_client("test.aoss.amazonaws.com")
        else:
            monkeypatch.setenv("OPENSEARCH_ENDPOINT", "test.aoss.amazonaws.com")
            client = build_vector_client()
        assert isinstance(client, RoutingVectorClient)
        assert isinstance(client._aurora, AuroraVectorClient)
        assert isinstance(client._aoss, OpenSearchVectorClient)
        assert client._aoss._endpoint == "test.aoss.amazonaws.com"

    def test_default_builds_opensearch_client_for_the_given_endpoint(self, monkeypatch):
        from coa_serve.clients.factory import build_vector_client
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
        monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
        client = build_vector_client("test.aoss.amazonaws.com")
        assert isinstance(client, OpenSearchVectorClient)
        assert client._endpoint == "test.aoss.amazonaws.com"

    def test_main_wires_the_vector_client_through_the_factory(self):
        # main imports bedrock_agentcore, so inspect its source instead of importing it.
        import ast
        from pathlib import Path

        import coa_serve

        tree = ast.parse((Path(coa_serve.__file__).parent / "main.py").read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "build_vector_client" in imported and "build_vector_client" in called
        assert "OpenSearchVectorClient" not in imported | called


@pytest.mark.unit
class TestClientSet:
    async def test_close_calls_client_close_methods(self):
        mock_graph = AsyncMock()
        mock_vector = AsyncMock()
        mock_llm = AsyncMock()
        mock_executor = AsyncMock()

        clients = ClientSet(
            graph=mock_graph,
            vector=mock_vector,
            llm=mock_llm,
            query_executor=mock_executor,
        )
        await clients.close()
        mock_graph.close.assert_called_once()
        mock_vector.close.assert_called_once()
        mock_llm.close.assert_called_once()
        mock_executor.close.assert_called_once()

    async def test_close_continues_on_error(self):
        mock_graph = AsyncMock()
        mock_graph.close = AsyncMock(side_effect=RuntimeError("graph close failed"))
        mock_vector = AsyncMock()
        mock_llm = AsyncMock()
        mock_executor = AsyncMock()

        clients = ClientSet(
            graph=mock_graph,
            vector=mock_vector,
            llm=mock_llm,
            query_executor=mock_executor,
        )
        await clients.close()
        mock_vector.close.assert_called_once()
        mock_llm.close.assert_called_once()
        mock_executor.close.assert_called_once()
