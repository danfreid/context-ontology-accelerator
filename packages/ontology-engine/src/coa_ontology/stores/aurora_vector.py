# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL (pgvector) vector store for the ontology workbench.

:class:`VectorStore` adapter over :class:`coa_common.aurora_search.AuroraSearchIndex`,
selected with ``WORKBENCH_BACKEND=aurora_pgvector``. It mirrors
:class:`~coa_ontology.stores.opensearch_vector.OpenSearchVectorStore`:

  - the search scope (the ``namespace`` column) is the same index name AOSS would
    use (:func:`_index_for_namespace`), so teardown and per-namespace isolation
    behave identically;
  - one document per ``(entity_uri, embedding_type, model_id, ontology_id)``;
    writes are idempotent upserts (unlike the AOSS append);
  - ``search_nearest`` returns raw cosine similarity in ``[-1, 1]``, as the
    :class:`VectorStore` protocol documents.

Reads never create the HNSW index. The schema migrator applies ``aurora-init.sql`` and owns
the HNSW index; writes only call ``ensure_vector_index`` to check that it exists.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from typing import Any

from coa_common.aurora_search import AuroraConfig, AuroraDataApiClient, AuroraSearchIndex, SearchDoc

from coa_ontology.stores.base import EmbeddingHit, VectorStore
from coa_ontology.stores.opensearch_vector import DEFAULT_NAMESPACE, OSS_DIMENSIONS, _index_for_namespace

log = logging.getLogger(__name__)

_pad_warned = False

DOC_KIND = "ontology"
_LIST_CAP = 10_000
_ENTITY_CAP = 1000
_PAGE_SIZE = 500
_URI_PAGE_SIZE = 2000
_DELETE_ENTITY_CHUNK = 500
_VECTOR_PAGE_SIZE = 25  # a 1024-dim vector is about 20 KB as text


def _vector_from_text(value: Any) -> list[float]:
    """Parse a pgvector text value (``[0.1,0.2]``) into a list of floats."""
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return [float(x) for x in parsed] if isinstance(parsed, list) else []
    return []


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


class AuroraVectorStore(VectorStore):
    """k-NN vector store backed by Aurora PostgreSQL with pgvector.

    Namespace handling matches the AOSS store: a bound namespace at
    construction, overridden per call.
    """

    def __init__(self, namespace: str | None = None):
        """Bind the store to a namespace and defer Data API client creation."""
        self.dimensions = OSS_DIMENSIONS  # replaced by AuroraConfig.dimensions once the index is built
        self._namespace = namespace
        self._index: AuroraSearchIndex | None = None
        self._client: AuroraDataApiClient | None = None
        self._build_lock = threading.Lock()

    def _aurora(self) -> AuroraSearchIndex:
        """Lazily build the Data API client and search index from the environment."""
        if self._index is None:
            with self._build_lock:
                if self._index is None:
                    cfg = AuroraConfig.from_env()
                    client = AuroraDataApiClient(cfg.cluster_arn, cfg.secret_arn, database=cfg.database)
                    self.dimensions = cfg.dimensions
                    self._client = client
                    self._index = AuroraSearchIndex(client, cfg.dimensions)
        return self._index

    def _resolve_ns(self, namespace: str | None) -> str | None:
        """Explicit call-site namespace wins; fall back to bound namespace."""
        return namespace if namespace is not None else self._namespace

    def _scope(self, namespace: str | None = None) -> str:
        """Scope (AOSS-equivalent index name) for a namespace. Never creates anything."""
        return _index_for_namespace(self._resolve_ns(namespace))

    def _write_index(self) -> AuroraSearchIndex:
        """Return the index after making sure the HNSW index exists. WRITE PATH ONLY."""
        index = self._aurora()
        index.ensure_vector_index()
        return index

    # Writes -----------------------------------------------------------

    def _padded(self, vector: Any) -> list[float]:
        """Zero-pad a short vector to ``self.dimensions`` (the VectorStore contract); warn once per process."""
        global _pad_warned
        vec = list(vector)
        if len(vec) < self.dimensions:
            if not _pad_warned:
                _pad_warned = True
                log.warning(
                    "zero-padding vectors shorter than the index dimension (%d < %d); further padding is not logged",
                    len(vec),
                    self.dimensions,
                )
            vec = vec + [0.0] * (self.dimensions - len(vec))
        return vec

    def _doc_from(self, item: dict, ns: str | None) -> SearchDoc:
        """Shape a store item into a :class:`SearchDoc`."""
        vec = self._padded(item["vector"])
        metadata: dict[str, Any] = {"namespace": ns or DEFAULT_NAMESPACE}
        if item.get("entity_type"):
            metadata["entity_type"] = item["entity_type"]
        for key in ("data_source_id", "query_backend", "context_text"):
            if item.get(key):
                metadata[key] = item[key]
        return SearchDoc(
            body=item.get("text", "") or "",
            doc_kind=DOC_KIND,
            entity_uri=item["entity_uri"],
            embedding_type=item["embedding_type"],
            model_id=item["model_id"],
            ontology_id=item.get("ontology_id") or "",
            embedding=vec,
            metadata=metadata,
            label=item.get("label"),
            aliases=tuple(item["aliases"] or ()) if "aliases" in item else None,
        )

    def store_embedding(self, data: dict) -> dict:
        """Upsert one embedding document (idempotent)."""
        ns = self._resolve_ns(data.get("namespace"))
        index = self._write_index()  # first: it sets self.dimensions from AuroraConfig
        index.upsert_docs(self._scope(ns), [self._doc_from(data, ns)])
        return self._echo(data, ns)

    def store_embeddings_batch(self, items: list[dict]) -> list[dict]:
        """Upsert a batch of embeddings, grouped by namespace."""
        if not items:
            return []
        by_ns: dict[str | None, list[dict]] = defaultdict(list)
        for it in items:
            by_ns[self._resolve_ns(it.get("namespace"))].append(it)
        out: list[dict] = []
        index = self._write_index()  # first: it sets self.dimensions from AuroraConfig
        for ns, ns_items in by_ns.items():
            index.upsert_docs(self._scope(ns), [self._doc_from(it, ns) for it in ns_items])
            out.extend(self._echo(it, ns) for it in ns_items)
        return out

    def _echo(self, item: dict, ns: str | None) -> dict:
        """Return the stored document in the shape the AOSS store returns."""
        vec = self._padded(item["vector"])
        doc = {
            "entity_uri": item["entity_uri"],
            "ontology_id": item.get("ontology_id"),
            "entity_type": item.get("entity_type"),
            "embedding_type": item["embedding_type"],
            "model_id": item["model_id"],
            "namespace": ns or DEFAULT_NAMESPACE,
            "text": item.get("text", ""),
            "embedding": vec,
        }
        for key in ("context_text", "data_source_id", "query_backend"):
            if item.get(key):
                doc[key] = item[key]
        return doc

    # Lookups ----------------------------------------------------------

    def _select(
        self,
        scope: str,
        containment: dict[str, Any],
        limit: int,
        *,
        with_vector: bool,
    ) -> list[dict]:
        """Metadata-containment select shaped like the AOSS ``filter_search`` hits."""
        self._aurora()
        assert self._client is not None  # set by _aurora()
        vector_col = ", CAST(embedding AS text) AS embedding" if with_vector else ""
        # The Data API caps a response at 1 MiB, so read in pages (halved on overflow).
        pages = self._client.iter_pages(
            f"SELECT doc_id, body, metadata{vector_col} FROM search_doc "
            "WHERE namespace = :ns AND doc_kind = :kind AND metadata @> CAST(:m AS jsonb) "
            "ORDER BY doc_id",
            {"ns": scope, "kind": DOC_KIND, "m": containment},
            page_size=_VECTOR_PAGE_SIZE if with_vector else _PAGE_SIZE,
            key_columns=("doc_id",),
        )
        out: list[dict] = []
        for page in pages:
            for r in page:
                meta = _metadata(r.get("metadata"))
                doc: dict[str, Any] = {**meta, "text": r.get("body", "") or ""}
                if with_vector:
                    doc["embedding"] = _vector_from_text(r.get("embedding"))
                out.append(doc)
                if len(out) >= limit:
                    return out
        return out

    def get_embeddings_for_entity(
        self,
        entity_uri,
        entity_type=None,
        embedding_type=None,
        namespace=None,
    ):
        """Return stored embeddings for one entity, optionally filtered."""
        m: dict[str, Any] = {"entity_uri": entity_uri}
        if entity_type:
            m["entity_type"] = entity_type
        if embedding_type:
            m["embedding_type"] = embedding_type
        return self._select(self._scope(namespace), m, _ENTITY_CAP, with_vector=True)

    def list_embeddings_for_ontology(
        self,
        ontology_id,
        entity_type=None,
        embedding_type=None,
        namespace=None,
        limit=None,
    ):
        """Return all embeddings for an ontology, optionally filtered.

        With ``limit`` set the vector is excluded, as in the AOSS store.
        """
        m: dict[str, Any] = {"ontology_id": ontology_id}
        if entity_type:
            m["entity_type"] = entity_type
        if embedding_type:
            m["embedding_type"] = embedding_type
        size = _LIST_CAP if limit is None else limit
        if size < 1:
            return []
        return self._select(self._scope(namespace), m, size, with_vector=limit is None)

    def list_searchable_entity_uris(self, ontology_id, namespace=None):
        """Return the entity URIs currently searchable for an ontology."""
        self._aurora()
        assert self._client is not None
        pages = self._client.iter_pages(
            "SELECT DISTINCT metadata ->> 'entity_uri' AS entity_uri FROM search_doc "
            "WHERE namespace = :ns AND doc_kind = :kind AND metadata @> CAST(:m AS jsonb) "
            "AND metadata ->> 'entity_uri' IS NOT NULL ORDER BY entity_uri",
            {"ns": self._scope(namespace), "kind": DOC_KIND, "m": {"ontology_id": ontology_id}},
            page_size=_URI_PAGE_SIZE,
            key_columns=("entity_uri",),
        )
        return [r["entity_uri"] for page in pages for r in page if r.get("entity_uri")]

    def search_nearest(
        self,
        vector,
        embedding_type,
        model_id,
        entity_type=None,
        ontology_id=None,
        top_k=10,
        namespace=None,
    ):
        """Cosine nearest-neighbour search; ``EmbeddingHit.score`` is raw cosine in [-1, 1]."""
        filters: dict[str, Any] = {"doc_kind": DOC_KIND, "embedding_type": embedding_type, "model_id": model_id}
        if entity_type:
            filters["entity_type"] = entity_type
        if ontology_id:
            filters["ontology_id"] = ontology_id
        raw = self._aurora().vector_search(self._scope(namespace), vector, top_k, filters)
        return [
            EmbeddingHit(
                entity_uri=h.metadata.get("entity_uri", ""),
                embedding_type=h.metadata.get("embedding_type", embedding_type),
                model_id=h.metadata.get("model_id", model_id),
                vector=[],
                ontology_id=h.metadata.get("ontology_id"),
                entity_type=h.metadata.get("entity_type"),
                score=h.score,
                text=h.body,
            )
            for h in raw
        ]

    def delete_embeddings_for_ontology(self, ontology_id, namespace=None) -> int:
        """Delete all embeddings for an ontology, returning the deleted count."""
        if not ontology_id:
            return 0
        return self._aurora().delete_docs(self._scope(namespace), filter_metadata={"ontology_id": ontology_id})

    def delete_embeddings_for_entities(
        self,
        entity_uris,
        ontology_id: str,
        namespace=None,
    ) -> int:
        """Delete the embeddings of the given entities within a single ontology.

        Always scoped by ``ontology_id`` so a sibling ontology that shares an IRI
        keeps its embedding.
        """
        uris = [u for u in dict.fromkeys(entity_uris) if u]
        if not uris or not ontology_id:
            return 0
        index = self._aurora()
        assert self._client is not None
        scope = self._scope(namespace)
        deleted = 0
        for start in range(0, len(uris), _DELETE_ENTITY_CHUNK):
            chunk = uris[start : start + _DELETE_ENTITY_CHUNK]
            doc_ids = [
                r["doc_id"]
                for page in self._client.iter_pages(
                    "SELECT doc_id FROM search_doc WHERE namespace = :ns AND doc_kind = :kind "
                    "AND metadata @> CAST(:m AS jsonb) "
                    "AND metadata ->> 'entity_uri' IN (SELECT jsonb_array_elements_text(CAST(:uris AS jsonb))) "
                    "ORDER BY doc_id",
                    {"ns": scope, "kind": DOC_KIND, "m": {"ontology_id": ontology_id}, "uris": chunk},
                    page_size=_URI_PAGE_SIZE,
                    key_columns=("doc_id",),
                )
                for r in page
            ]
            if doc_ids:
                # One call per chunk; the ontology_id filter stays so a sibling ontology is never touched.
                deleted += index.delete_docs(scope, filter_metadata={"ontology_id": ontology_id}, doc_ids=doc_ids)
        return deleted

    def delete_index(self, namespace: str | None = None) -> bool:
        """Delete every document of the namespace's scope. Returns True if any existed."""
        return self._aurora().delete_scope(self._scope(namespace)) > 0

    # Health -----------------------------------------------------------

    def health_check(self) -> dict:
        """Probe Aurora with ``SELECT 1`` through the Data API."""
        try:
            self._aurora()
            assert self._client is not None
            self._client.execute("SELECT 1 AS ok")
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            log.warning("aurora health check failed: %s", type(exc).__name__)
            return {"status": "error", "error": "connectivity check failed"}
