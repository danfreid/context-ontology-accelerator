# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL (pgvector + tsvector) client — VectorClient implementation.

Counterpart of :class:`~coa_serve.clients.opensearch.OpenSearchVectorClient`,
selected when ``coa_common.resolve_search_backend()`` returns PGVECTOR. It is a
thin async wrapper over :class:`coa_common.aurora_search.AuroraSearchIndex`; the
shared sync client runs in a thread executor, like the AOSS client.

Scores: ``VectorHit.score`` is ``(1 + cosine) / 2`` so thresholds written
against the AOSS ``cosinesimil`` score in ``[0, 1]`` keep their meaning.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from coa_common.aurora_search import (
    AuroraConfig,
    AuroraDataApiClient,
    AuroraSearchIndex,
    SearchHit,
    SearchMode,
)
from coa_common.constants import StructuredBackend

from .base import VectorClient, VectorHit
from .opensearch import _nested_source, _source_document_id, _source_document_name

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aurora")


def _toolkit_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the GraphRAG node metadata (`source` / `chunk`) of a stored document.

    The document's `metadata` column holds it either directly or under a nested
    `metadata` key (the full AOSS `_source` shape); both are accepted.
    """
    nested = metadata.get("metadata")
    if isinstance(nested, dict) and ("source" in nested or "chunk" in nested):
        return nested
    return metadata


class AuroraVectorClient(VectorClient):
    """Aurora pgvector k-NN, BM25 phrase and hybrid search client (async)."""

    def __init__(self, index: str = "document-chunks", search_index: AuroraSearchIndex | None = None):
        """Configure the default scope and optionally inject a ready ``AuroraSearchIndex``.

        Args:
            index: Default scope (the AOSS-equivalent index name) for searches.
            search_index: Pre-built index, mainly for tests. When omitted it is
                built lazily from the environment (see ``AuroraConfig.from_env``).
        """
        self._index = index
        self._search_index = search_index
        self._client: Any = getattr(search_index, "_client", None)
        self._build_lock = threading.Lock()

    def _aurora(self) -> AuroraSearchIndex:
        """Lazily build the shared search index so construction needs no AWS access."""
        if self._search_index is None:
            with self._build_lock:
                if self._search_index is None:
                    cfg = AuroraConfig.from_env()
                    client = AuroraDataApiClient(cfg.cluster_arn, cfg.secret_arn, database=cfg.database)
                    self._client = client
                    self._search_index = AuroraSearchIndex(client, cfg.dimensions)
        return self._search_index

    async def _run(self, fn, *args, **kwargs):
        """Run a sync shared-client call in the thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))

    @staticmethod
    def _backend_filter(backend: StructuredBackend) -> dict[str, Any]:
        """Filter keeping only documents answered by ``backend``.

        Mirrors ``OpenSearchVectorClient._backend_filter``: ``query_backend`` is written
        on graph (``RDF_SPARQL``) class documents only, so an absent key means SQL.
        Returns a filter dict understood by ``AuroraSearchIndex``.
        """
        if backend == StructuredBackend.RDF_SPARQL:
            return {"query_backend": StructuredBackend.RDF_SPARQL.value}
        return {"exclude_metadata": {"query_backend": StructuredBackend.RDF_SPARQL.value}}

    @staticmethod
    def _filters(
        entity_type: str | None,
        require_mapped: bool,
        ontology_ids: list[str] | None,
        query_backend: StructuredBackend | None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if entity_type:
            filters["entity_type"] = entity_type
        if require_mapped:
            filters["require_mapped"] = True
        if ontology_ids:
            filters["ontology_ids"] = list(ontology_ids)
        if query_backend is not None:
            filters.update(AuroraVectorClient._backend_filter(query_backend))
        return filters

    @staticmethod
    def _to_hit(hit: SearchHit, score: float) -> VectorHit:
        """Shape a search hit into a VectorHit through the same source dict the AOSS path builds."""
        source = {**hit.metadata, "metadata": _toolkit_metadata(hit.metadata), "text": hit.body}
        return VectorHit(
            id=hit.doc_id,
            text=source.get("text", "") or source.get("value", ""),
            score=score,
            metadata={
                "source_doc": _source_document_id(source),
                "source_doc_name": _source_document_name(source),
                "chunk_index": source.get("chunk_index"),
                "namespace": source.get("namespace", ""),
                "uri": source.get("uri", source.get("entity_uri", "")),
                "type": source.get("type", source.get("entity_type", "")),
                "label": source.get("label", source.get("text", "")),
                "description": source.get("description", ""),
                "entity_type": source.get("entity_type", ""),
                "entity_uri": source.get("entity_uri", ""),
                "data_source_id": source.get("data_source_id", ""),
                "query_backend": source.get("query_backend", ""),
                "context_text": source.get("context_text", ""),
            },
        )

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
        """Cosine k-NN search, optionally filtered by entity_type / mapped-only / ontology / backend."""
        filters = self._filters(entity_type, require_mapped, ontology_ids, query_backend)
        hits = await self._run(self._aurora().vector_search, index or self._index, query_vector, top_k, filters or None)
        return [self._to_hit(h, (1.0 + h.score) / 2.0) for h in hits]

    async def search_hybrid(
        self,
        query_text: str,
        query_vector: list[float] | None,
        *,
        index: str | None = None,
        top_k: int = 5,
        mode: SearchMode = SearchMode.HYBRID,
        entity_type: str | None = None,
        require_mapped: bool = False,
        ontology_ids: list[str] | None = None,
        query_backend: StructuredBackend | None = None,
    ) -> list[VectorHit]:
        """Fuse vector, BM25 and trigram rankings (RRF). ``score`` is the fused RRF score.

        Not used by any existing caller; available to callers that want fusion.
        """
        filters = self._filters(entity_type, require_mapped, ontology_ids, query_backend)
        hits = await self._run(
            self._aurora().search,
            index or self._index,
            query_text,
            query_vector,
            mode,
            top_k,
            filters or None,
        )
        return [self._to_hit(h, h.score) for h in hits]

    async def phrase_search(self, phrases: list[str], *, index: str, size: int = 50) -> list[dict[str, Any]]:
        """Keyword search for exact phrases over the chunk text.

        Each phrase uses ``phraseto_tsquery`` so phrase semantics match the AOSS
        ``match_phrase`` clauses. Returns one dict per hit: ``chunk_id``,
        ``source_id``, ``source_s3_key``, ``text``, ``score`` and ``matched``
        (indexes into ``phrases``).
        """
        wanted = {i: " ".join(p.split()) for i, p in enumerate(phrases) if p and p.strip()}
        if not wanted:
            return []
        hits = await self._run(
            self._aurora().keyword_search,
            index,
            "",
            size,
            phrases=list(dict.fromkeys(wanted.values())),
        )
        out: list[dict[str, Any]] = []
        for hit in hits:
            metadata = _toolkit_metadata(hit.metadata)
            chunk_meta = metadata.get("chunk") if isinstance(metadata.get("chunk"), dict) else {}
            nested = _nested_source({"metadata": metadata})
            nested_meta = nested.get("metadata") if isinstance(nested.get("metadata"), dict) else {}
            matched_text = set(hit.matched_phrases)
            out.append(
                {
                    "chunk_id": chunk_meta.get("chunkId") or hit.doc_id,
                    "source_id": nested.get("sourceId", "") or "",
                    "source_s3_key": nested_meta.get("source_s3_key", "") or "",
                    "text": hit.body,
                    "score": hit.score,
                    "matched": [i for i, p in wanted.items() if p in matched_text],
                }
            )
        return out

    def _probe(self) -> None:
        self._aurora()
        assert self._client is not None
        self._client.execute("SELECT 1 AS ok")

    async def health_check(self) -> dict[str, Any]:
        """Check Aurora connectivity (serve ``healthy``/``unhealthy`` shape); never raises."""
        try:
            await self._run(self._probe)
            return {"status": "healthy", "via": "rds_data_api"}
        except Exception as e:  # noqa: BLE001 — health probe must not raise
            logger.warning("health_check_failed", error=str(e))
            return {"status": "unhealthy", "error": "connectivity check failed"}

    async def close(self) -> None:
        """No-op close kept for VectorClient interface compatibility."""
        return None
