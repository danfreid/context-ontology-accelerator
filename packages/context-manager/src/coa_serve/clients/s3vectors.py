# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""S3 Vectors client — VectorClient implementation for the GraphRAG ``chunk_``/``topic_``/``statement_`` scopes.

Selected on the ckg path (``GRAPH_BACKEND=aurora`` and ``GRAPHRAG_VECTOR_BACKEND=s3vectors``), where the
toolkit writes its vector indexes to an S3 vector bucket instead of OpenSearch Serverless.

* **Index names** follow the toolkit's ``S3VectorIndex.underlying_index_name``:
  ``<prefix>.<index with '_' replaced by '-'>``, e.g. ``chunk_<tenant>`` -> ``graphrag.chunk-<tenant>``.
* **Search** calls ``QueryVectors`` (``topK`` is capped at 100, the service limit). The score is
  ``1 - distance / 2`` so cosine distance in ``[0, 2]`` maps to ``[0, 1]``, the scale of the Aurora and AOSS
  clients.
* **Filters** are the OpenSearch-style dicts COA builds for ``AossVectorClient.knn_search`` (``term``, ``terms``,
  ``exists``, ``range``, ``bool.must/filter/should``). They translate to the S3 Vectors ``$eq/$in/$exists/$gt..``
  and ``$and/$or`` syntax. S3 Vectors has no NOT, so ``must_not`` and any other construct raise
  :class:`UnsupportedS3VectorsFilter`; the fields ``value`` and ``metadata`` are stored non-filterable.
* **phrase_search** has no S3 Vectors equivalent (no keyword leg), so it runs against the chunk text in the
  Aurora document graph (``coa_common.graphrag_pg.phrase``), which holds the same chunks.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from coa_common import resolve_region, sync_boto_config
from coa_common.constants import (
    GRAPHRAG_AURORA_SCHEMA_ENV,
    GRAPHRAG_INDEX_PREFIXES,
    GRAPHRAG_S3_VECTOR_BUCKET_ENV,
    GRAPHRAG_S3_VECTOR_PREFIX_ENV,
    StructuredBackend,
)

from .base import VectorClient, VectorHit

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="s3vectors")

MAX_TOP_K = 100
_NON_FILTERABLE = frozenset({"value", "metadata"})
_SOURCE_METADATA_PREFIX = "source.metadata."
_RANGE_OPS = {"gt": "$gt", "gte": "$gte", "lt": "$lt", "lte": "$lte"}
_SCOPE_PREFIXES = tuple(f"{p}_" for p in GRAPHRAG_INDEX_PREFIXES)


class UnsupportedS3VectorsFilter(ValueError):
    """A filter uses a construct S3 Vectors cannot express (NOT, wildcard, full-text, ...)."""


def _field(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise UnsupportedS3VectorsFilter(f"S3 Vectors filter needs a field name, got {name!r}")
    if name in _NON_FILTERABLE:
        raise UnsupportedS3VectorsFilter(f"S3 Vectors metadata field {name!r} is not filterable")
    return name


def _single(clause: dict[str, Any]) -> tuple[str, Any]:
    if len(clause) != 1:
        raise UnsupportedS3VectorsFilter(f"expected one key in filter clause, got {sorted(clause)}")
    return next(iter(clause.items()))


def _combine(op: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return parts[0] if len(parts) == 1 else {op: parts}


def translate_filter(clause: dict[str, Any]) -> dict[str, Any]:
    """Translate one OpenSearch-style filter clause to an S3 Vectors metadata filter.

    Raises:
        UnsupportedS3VectorsFilter: For NOT (``must_not``) and every construct without an S3 Vectors operator.
    """
    kind, body = _single(clause)
    if kind == "term":
        field, value = _single(body)
        return {_field(field): {"$eq": value}}
    if kind == "terms":
        field, values = _single(body)
        if not isinstance(values, list):
            raise UnsupportedS3VectorsFilter("terms filter needs a list of values")
        return {_field(field): {"$in": list(values)}}
    if kind == "exists":
        return {_field(body.get("field")): {"$exists": True}}
    if kind == "range":
        field, bounds = _single(body)
        ops = {_RANGE_OPS[k]: v for k, v in bounds.items() if k in _RANGE_OPS}
        if not ops or len(ops) != len(bounds):
            raise UnsupportedS3VectorsFilter(f"unsupported range bounds {sorted(bounds)}")
        return {_field(field): ops}
    if kind == "bool":
        return _translate_bool(body)
    raise UnsupportedS3VectorsFilter(f"S3 Vectors cannot express a {kind!r} filter")


def _translate_bool(body: dict[str, Any]) -> dict[str, Any]:
    unknown = set(body) - {"must", "filter", "should", "must_not", "minimum_should_match"}
    if unknown:
        raise UnsupportedS3VectorsFilter(f"unsupported bool keys {sorted(unknown)}")
    if body.get("must_not"):
        raise UnsupportedS3VectorsFilter("S3 Vectors has no NOT: must_not filters are not supported")

    def clauses(key: str) -> list[dict[str, Any]]:
        raw = body.get(key) or []
        return [translate_filter(c) for c in (raw if isinstance(raw, list) else [raw])]

    musts = clauses("must") + clauses("filter")
    shoulds = clauses("should")
    parts = list(musts)
    if shoulds:
        if body.get("minimum_should_match", 1) != 1:
            raise UnsupportedS3VectorsFilter("only minimum_should_match=1 is supported")
        parts.append(_combine("$or", shoulds))
    if not parts:
        raise UnsupportedS3VectorsFilter("empty bool filter")
    return _combine("$and", parts)


def translate_filters(filters: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Translate the filter list COA passes to a k-NN search (AND of clauses); ``None`` when there are none."""
    if not filters:
        return None
    return _combine("$and", [translate_filter(f) for f in filters])


def s3_index_name(index: str, prefix: str) -> str:
    """Underlying S3 Vectors index name for a COA scope (``chunk_<tenant>``) under ``prefix``."""
    name = index.replace("_", "-")
    if prefix:
        name = f"{prefix}.{name}"
    if not 3 <= len(name) <= 63:
        raise ValueError(f"S3 Vectors index names must be 3-63 characters: {name}")
    return name


def _to_hit(vector: dict[str, Any]) -> VectorHit:
    """Shape a ``QueryVectors`` result into a ``VectorHit`` (same metadata keys the AOSS/Aurora clients set)."""
    metadata = vector.get("metadata") or {}
    node_meta = json.loads(metadata.get("metadata") or "{}")
    source = node_meta.get("source") if isinstance(node_meta.get("source"), dict) else {}
    source_meta = {
        k[len(_SOURCE_METADATA_PREFIX) :]: v for k, v in metadata.items() if k.startswith(_SOURCE_METADATA_PREFIX)
    }
    s3_key = source_meta.get("source_s3_key")
    name = s3_key.rsplit("/", 1)[-1] if isinstance(s3_key, str) and s3_key else source_meta.get("filename", "")
    text = metadata.get("value", "") or ""
    distance = float(vector.get("distance", 0.0) or 0.0)
    return VectorHit(
        id=vector.get("key", "") or "",
        text=text,
        score=max(0.0, min(1.0, 1.0 - distance / 2.0)),
        metadata={
            "source_doc": source.get("sourceId", "") or "",
            "source_doc_name": name if isinstance(name, str) else "",
            "chunk_index": None,
            "namespace": "",
            "uri": "",
            "type": "",
            "label": text,
            "description": "",
            "entity_type": "",
            "entity_uri": "",
            "data_source_id": "",
            "query_backend": "",
            "context_text": "",
        },
    )


class S3VectorsClient(VectorClient):
    """S3 Vectors k-NN client for the GraphRAG ``chunk_``/``topic_``/``statement_`` scopes (async)."""

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str | None = None,
        region: str | None = None,
        *,
        s3vectors: Any | None = None,
        aurora_client: Any | None = None,
        schema: str | None = None,
    ):
        """Configure the vector bucket; clients are built lazily so construction needs no AWS access.

        Args:
            bucket: S3 vector bucket. Defaults to ``GRAPHRAG_S3_VECTOR_BUCKET``.
            prefix: Index-name prefix. Defaults to ``GRAPHRAG_S3_VECTOR_PREFIX`` or ``graphrag``.
            region: AWS region. Defaults to the resolved region.
            s3vectors: Injected boto3 ``s3vectors`` client (tests).
            aurora_client: Injected Data API client for ``phrase_search`` (tests).
            schema: Aurora schema of the document graph. Defaults to ``GRAPHRAG_AURORA_SCHEMA`` or ``lg``.

        Raises:
            ValueError: If no bucket is configured.
        """
        self._bucket = bucket or os.environ.get(GRAPHRAG_S3_VECTOR_BUCKET_ENV, "").strip()
        if not self._bucket:
            raise ValueError(f"S3 vector bucket must be given or set in {GRAPHRAG_S3_VECTOR_BUCKET_ENV}")
        if prefix is None:
            prefix = os.environ.get(GRAPHRAG_S3_VECTOR_PREFIX_ENV, "").strip().strip("/") or "graphrag"
        self._prefix = prefix
        self._region = region
        self._client = s3vectors
        self._aurora = aurora_client
        self._schema = schema or os.environ.get(GRAPHRAG_AURORA_SCHEMA_ENV, "").strip() or "lg"

    def _s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3vectors", region_name=self._region or resolve_region(), config=sync_boto_config()
            )
        return self._client

    def _aurora_client(self) -> Any:
        if self._aurora is None:
            from coa_common.aurora_data_api import build_client_from_env

            self._aurora = build_client_from_env()
        return self._aurora

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))

    @staticmethod
    def _require_scope(index: str | None) -> str:
        if not index or not index.startswith(_SCOPE_PREFIXES):
            raise ValueError(f"S3 Vectors serves only {', '.join(_SCOPE_PREFIXES)}* scopes, got {index!r}")
        return index

    def _query(self, index: str, query_vector: list[float], top_k: int, flt: dict[str, Any] | None) -> list[VectorHit]:
        params: dict[str, Any] = {
            "vectorBucketName": self._bucket,
            "indexName": s3_index_name(index, self._prefix),
            "queryVector": {"float32": [float(x) for x in query_vector]},
            "topK": max(1, min(int(top_k), MAX_TOP_K)),
            "returnDistance": True,
            "returnMetadata": True,
        }
        if flt:
            params["filter"] = flt
        response = self._s3().query_vectors(**params)
        return [_to_hit(v) for v in response.get("vectors", [])]

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
        filters: list[dict[str, Any]] | None = None,
    ) -> list[VectorHit]:
        """Cosine k-NN over a GraphRAG scope; ``filters`` are OpenSearch-style clauses (see :func:`translate_filters`).

        The ontology-entity arguments (``entity_type``, ``require_mapped``, ``ontology_ids``, ``query_backend``)
        describe documents these indexes do not hold, so passing one raises instead of silently returning
        unfiltered results.
        """
        scope = self._require_scope(index)
        if entity_type or require_mapped or ontology_ids or query_backend is not None:
            raise UnsupportedS3VectorsFilter(
                "entity_type, require_mapped, ontology_ids and query_backend do not apply to GraphRAG scopes"
            )
        flt = translate_filters(filters)
        return await self._run(self._query, scope, query_vector, top_k, flt)

    async def search_hybrid(
        self,
        query_text: str,
        query_vector: list[float] | None,
        *,
        index: str | None = None,
        top_k: int = 5,
        mode: Any = None,
        **kwargs: Any,
    ) -> list[VectorHit]:
        """Vector-only search: S3 Vectors has no keyword leg, so ``query_text`` is not used.

        Use :meth:`phrase_search` for keyword matching over chunk text.
        """
        if query_vector is None:
            raise ValueError("S3 Vectors search needs a query_vector (it has no keyword leg)")
        return await self.search(query_vector, top_k=top_k, index=index, **kwargs)

    async def phrase_search(self, phrases: list[str], *, index: str, size: int = 50) -> list[dict[str, Any]]:
        """Exact-phrase search over the chunk text held in the Aurora document graph.

        Same return shape as ``OpenSearchVectorClient.phrase_search``. Only ``chunk_<tenant>`` scopes exist.
        """
        from coa_common.graphrag_pg.phrase import phrase_search

        if not index or not index.startswith("chunk_"):
            raise ValueError(f"phrase_search needs a chunk_<tenant> scope, got {index!r}")
        tenant = index[len("chunk_") :]
        return await self._run(phrase_search, self._aurora_client(), self._schema, tenant, phrases, size)

    async def health_check(self) -> dict[str, Any]:
        """Check the vector bucket is reachable (serve ``healthy``/``unhealthy`` shape); never raises."""
        try:
            await self._run(self._s3().get_vector_bucket, vectorBucketName=self._bucket)
            return {"status": "healthy", "via": "s3vectors"}
        except Exception as e:  # noqa: BLE001 — health probe must not raise
            logger.warning("health_check_failed", error=str(e))
            return {"status": "unhealthy", "error": "connectivity check failed"}

    async def close(self) -> None:
        """No-op close kept for VectorClient interface compatibility."""
        return None
