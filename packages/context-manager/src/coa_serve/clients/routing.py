# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Routing VectorClient for the Aurora search path.

On the Aurora path ontology and entity documents live in Aurora, but the
GraphRAG toolkit still writes its ``chunk_*``, ``topic_*`` and ``statement_*``
indexes to OpenSearch Serverless. This client sends reads for those scopes to
AOSS and every other scope to Aurora.

On the ckg path (Aurora document graph + S3 Vectors) the GraphRAG scopes are in an S3 vector bucket, so
the factory passes an ``s3vectors`` client and those scopes go there instead of AOSS.
"""

from __future__ import annotations

from typing import Any

import structlog
from coa_common.constants import GRAPHRAG_INDEX_PREFIXES, StructuredBackend

from .base import VectorClient, VectorHit

logger = structlog.get_logger(__name__)
_AOSS_PREFIXES = tuple(f"{prefix}_" for prefix in GRAPHRAG_INDEX_PREFIXES)


def _is_graphrag_index(index: str | None) -> bool:
    """True when ``index`` names a GraphRAG toolkit index held in AOSS."""
    return bool(index) and str(index).startswith(_AOSS_PREFIXES)


class RoutingVectorClient(VectorClient):
    """Send GraphRAG index scopes to AOSS (or S3 Vectors on the ckg path) and all other scopes to Aurora."""

    def __init__(self, aurora: Any, aoss: Any, s3vectors: Any | None = None):
        """Wrap the Aurora client (default route) and the client for the GraphRAG scopes.

        ``s3vectors`` replaces ``aoss`` for the GraphRAG scopes when given (ckg path); ``aoss`` may then be ``None``.
        """
        self._aurora = aurora
        self._aoss = aoss
        self._s3vectors = s3vectors

    def _graphrag(self) -> Any:
        return self._s3vectors if self._s3vectors is not None else self._aoss

    def _route(self, index: str | None) -> Any:
        return self._graphrag() if _is_graphrag_index(index) else self._aurora

    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str = "",
        top_k: int = 5,
        index: str | None = None,
        entity_type: str | None = None,
        require_mapped: bool = False,
        ontology_ids: list[str] | None = None,
        query_backend: StructuredBackend | None = None,
    ) -> list[VectorHit]:
        """k-NN search on the backend that holds ``index``."""
        result: list[VectorHit] = await self._route(index).search(
            query_vector,
            namespace=namespace,
            top_k=top_k,
            index=index,
            entity_type=entity_type,
            require_mapped=require_mapped,
            ontology_ids=ontology_ids,
            query_backend=query_backend,
        )
        return result

    async def search_hybrid(self, *args: Any, **kwargs: Any) -> list[VectorHit]:
        """Hybrid (vector + BM25 + trigram) search; Aurora implements it, or S3 Vectors for its scopes (vector-only)."""
        index = kwargs.get("index")
        target = self._s3vectors if self._s3vectors is not None and _is_graphrag_index(index) else self._aurora
        result: list[VectorHit] = await target.search_hybrid(*args, **kwargs)
        return result

    async def phrase_search(self, phrases: list[str], *, index: str, size: int = 50) -> list[dict[str, Any]]:
        """Exact-phrase search on the backend that holds ``index``."""
        result: list[dict[str, Any]] = await self._route(index).phrase_search(phrases, index=index, size=size)
        return result

    async def health_check(self) -> dict[str, Any]:
        """Aurora's health result plus an ``aoss`` (or ``s3vectors``) sub-status; never changes the top-level one."""
        result = dict(await self._aurora.health_check())
        key = "s3vectors" if self._s3vectors is not None else "aoss"
        try:
            result[key] = await self._graphrag().health_check()
        except Exception as e:  # noqa: BLE001 — health probe must not raise
            logger.warning(f"{key}_health_check_failed", error=str(e))
            result[key] = {
                "status": "unhealthy",
                "error": "connectivity check failed",
            }
        return result

    async def close(self) -> None:
        """Close both clients."""
        try:
            await self._aurora.close()
        finally:
            await self._graphrag().close()
