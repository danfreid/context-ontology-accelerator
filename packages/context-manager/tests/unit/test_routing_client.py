# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for RoutingVectorClient (GraphRAG scopes to AOSS, everything else to Aurora)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_common.constants import graphrag_index_names, ontology_vector_index_name
from coa_serve.clients.base import VectorClient
from coa_serve.clients.routing import RoutingVectorClient

pytestmark = pytest.mark.unit

NS = "550e8400-e29b-41d4-a716-446655440000"


def _client() -> tuple[RoutingVectorClient, MagicMock, MagicMock]:
    aurora, aoss = MagicMock(), MagicMock()
    for c, name in ((aurora, "aurora"), (aoss, "aoss")):
        c.search = AsyncMock(return_value=[name])
        c.phrase_search = AsyncMock(return_value=[{"from": name}])
        c.search_hybrid = AsyncMock(return_value=[name])
        c.health_check = AsyncMock(return_value={"status": "healthy", "via": name})
        c.close = AsyncMock()
    return RoutingVectorClient(aurora, aoss), aurora, aoss


def test_is_a_vector_client() -> None:
    assert isinstance(_client()[0], VectorClient)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", graphrag_index_names(NS))
async def test_graphrag_scopes_go_to_aoss(index: str) -> None:
    client, aurora, aoss = _client()
    assert await client.search([0.1], index=index, top_k=3) == ["aoss"]
    assert await client.phrase_search(["a b"], index=index, size=7) == [{"from": "aoss"}]
    aoss.search.assert_awaited_once()
    assert aoss.search.await_args.kwargs["index"] == index
    assert aoss.search.await_args.kwargs["top_k"] == 3
    aoss.phrase_search.assert_awaited_once_with(["a b"], index=index, size=7)
    aurora.search.assert_not_awaited()
    aurora.phrase_search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index",
    [
        None,
        "",
        "document-chunks",
        "chunked_x",
        "chunk-abc",
        ontology_vector_index_name("chunk", NS),
        ontology_vector_index_name("topic", NS),
        ontology_vector_index_name("statement", NS),
        ontology_vector_index_name("ontology-workbench-embeddings", NS),
    ],
)
async def test_other_scopes_go_to_aurora(index: str | None) -> None:
    client, aurora, aoss = _client()
    assert await client.search([0.1], index=index) == ["aurora"]
    aurora.search.assert_awaited_once()
    aoss.search.assert_not_awaited()
    if index:
        assert await client.phrase_search(["a"], index=index) == [{"from": "aurora"}]
        aoss.phrase_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_ontology_vector_index_name_never_matches_the_prefixes() -> None:
    client, aurora, aoss = _client()
    for prefix in ("chunk", "topic", "statement"):
        for ns in (NS, "default"):
            await client.search([0.1], index=ontology_vector_index_name(prefix, ns))
    assert aurora.search.await_count == 6
    aoss.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_hybrid_always_uses_aurora() -> None:
    client, aurora, aoss = _client()
    assert await client.search_hybrid("q", [0.1], index=graphrag_index_names(NS)[0]) == ["aurora"]
    aoss.search_hybrid.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_is_aurora_plus_aoss_substatus() -> None:
    client, _, _ = _client()
    assert await client.health_check() == {
        "status": "healthy",
        "via": "aurora",
        "aoss": {"status": "healthy", "via": "aoss"},
    }


@pytest.mark.asyncio
async def test_aoss_failure_does_not_change_top_level_status() -> None:
    client, aurora, aoss = _client()
    aoss.health_check.return_value = {"status": "unhealthy", "error": "x"}
    assert (await client.health_check())["status"] == "healthy"
    aoss.health_check.side_effect = RuntimeError("boom")
    result = await client.health_check()
    assert result["status"] == "healthy"
    assert result["aoss"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_aurora_unhealthy_is_reported() -> None:
    client, aurora, _ = _client()
    aurora.health_check.return_value = {"status": "unhealthy", "error": "x"}
    assert (await client.health_check())["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_close_closes_both_even_if_one_fails() -> None:
    client, aurora, aoss = _client()
    await client.close()
    aurora.close.assert_awaited_once()
    aoss.close.assert_awaited_once()
    aurora.close.side_effect = RuntimeError("x")
    with pytest.raises(RuntimeError):
        await client.close()
    assert aoss.close.await_count == 2
