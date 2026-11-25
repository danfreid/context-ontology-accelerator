# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL + pgvector implementation of :class:`VectorStore`.

One table, ``coa_embedding``, holds every namespace. A row is unique on
``(namespace, ontology_id, entity_uri, embedding_type, model_id)`` and is written
with ``ON CONFLICT DO UPDATE``, so re-storing an embedding replaces it. (The
OpenSearch store appends duplicates; callers that delete first behave the same.)

``search_nearest`` returns raw cosine similarity in ``[-1, 1]`` (``1 - (a <=> b)``),
as ``stores.base`` requires.

Data API constraints handled here: no array parameters (lists go in as one JSON
string), vectors travel as text (``CAST(CAST(:v AS text) AS vector)`` in,
``embedding::text`` out), bulk writes send one JSON string per chunk, large reads
page by keyset and halve the page at the 1 MiB response cap, and deletes run in
bounded batches under the 45 second statement limit.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

from coa_common.aurora_data_api import (
    AuroraConfigError,
    DataApiClient,
    ResponseTooLargeError,
    bulk_json_chunks,
    json_text,
)

from coa_ontology.stores.aurora.schema import SchemaInfo, configured_dimensions, default_client, ensure_schema
from coa_ontology.stores.base import EmbeddingHit, VectorStore

log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")
# Mirrors OpenSearchVectorStore.list_embeddings_for_ontology when the caller gives no limit.
_UNLIMITED_LIST_CAP = 10_000
_DELETE_BATCH = 5000
_ENTITY_CHUNK = 1000
_DEFAULT_EF_SEARCH = 40
_MAX_EF_SEARCH = 1000

_DOC_COLUMNS = (
    "id, entity_uri, ontology_id, entity_type, embedding_type, model_id, namespace, text, "
    "context_text, data_source_id, query_backend, embedding::text AS embedding"
)
_UPSERT = """
INSERT INTO coa_embedding
  (namespace, ontology_id, entity_uri, entity_type, embedding_type, model_id,
   text, context_text, data_source_id, query_backend, embedding)
SELECT r.namespace, r.ontology_id, r.entity_uri, r.entity_type, r.embedding_type, r.model_id,
       r.text, r.context_text, r.data_source_id, r.query_backend, CAST(r.embedding AS vector)
FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
  namespace text, ontology_id text, entity_uri text, entity_type text, embedding_type text, model_id text,
  text text, context_text text, data_source_id text, query_backend text, embedding text)
ON CONFLICT (namespace, ontology_id, entity_uri, embedding_type, model_id) DO UPDATE SET
  entity_type = EXCLUDED.entity_type, text = EXCLUDED.text, context_text = EXCLUDED.context_text,
  data_source_id = EXCLUDED.data_source_id, query_backend = EXCLUDED.query_backend,
  embedding = EXCLUDED.embedding
"""


def vector_text(vec: Iterable[float]) -> str:
    """Render a vector as pgvector text input (``[0.1,0.2]``)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class AuroraVectorStore(VectorStore):
    """k-NN vector store on Aurora PostgreSQL with pgvector (HNSW, cosine).

    The namespace may be bound at construction or passed per call; a per-call value wins.
    """

    def __init__(
        self,
        namespace: str | None = None,
        *,
        client: DataApiClient | None = None,
        dimensions: int | None = None,
    ):
        """Bind a namespace; the Data API client and schema check are deferred to first use."""
        self._namespace = namespace
        self._client = client
        self.dimensions = dimensions or configured_dimensions()
        self._info: SchemaInfo | None = None

    # ── plumbing ────────────────────────────────────────────────────

    def _db(self) -> DataApiClient:
        """Return the client after the once-per-process schema check."""
        if self._client is None:
            self._client = default_client()
        if self._info is None:
            self._info = ensure_schema(self._client, self.dimensions)
        return self._client

    def _resolve_ns(self, namespace: str | None) -> str:
        """Explicit call-site namespace wins; fall back to the bound one, then the default."""
        ns = namespace if namespace is not None else self._namespace
        return ns or DEFAULT_NAMESPACE

    def _padded(self, vec: list[float]) -> list[float]:
        if len(vec) > self.dimensions:
            raise ValueError(f"Vector has {len(vec)} dimensions, expected at most {self.dimensions}")
        if len(vec) < self.dimensions:
            return list(vec) + [0.0] * (self.dimensions - len(vec))
        return list(vec)

    def _doc_from(self, item: dict, ns: str) -> dict:
        """Shape a store item into the document returned to callers (same keys as the OpenSearch store)."""
        doc = {
            "entity_uri": item["entity_uri"],
            "ontology_id": item.get("ontology_id"),
            "entity_type": item.get("entity_type"),
            "embedding_type": item["embedding_type"],
            "model_id": item["model_id"],
            "namespace": ns,
            "text": item.get("text", ""),
            "embedding": self._padded(item["vector"]),
        }
        if item.get("context_text"):
            doc["context_text"] = item["context_text"]
        if item.get("data_source_id"):
            doc["data_source_id"] = item["data_source_id"]
        if item.get("query_backend"):
            doc["query_backend"] = item["query_backend"]
        return doc

    @staticmethod
    def _row_from_doc(doc: dict) -> dict:
        return {
            "namespace": doc["namespace"],
            "ontology_id": doc.get("ontology_id") or "",
            "entity_uri": doc["entity_uri"],
            "entity_type": doc.get("entity_type"),
            "embedding_type": doc["embedding_type"],
            "model_id": doc["model_id"],
            "text": doc.get("text") or "",
            "context_text": doc.get("context_text"),
            "data_source_id": doc.get("data_source_id"),
            "query_backend": doc.get("query_backend"),
            "embedding": vector_text(doc["embedding"]),
        }

    @staticmethod
    def _to_doc(row: Mapping[str, Any]) -> dict:
        doc: dict[str, Any] = {
            "entity_uri": row["entity_uri"],
            "ontology_id": row.get("ontology_id") or None,
            "entity_type": row.get("entity_type"),
            "embedding_type": row["embedding_type"],
            "model_id": row["model_id"],
            "namespace": row["namespace"],
            "text": row.get("text") or "",
            "embedding": json.loads(row["embedding"]) if row.get("embedding") else [],
        }
        for key in ("context_text", "data_source_id", "query_backend"):
            if row.get(key):
                doc[key] = row[key]
        return doc

    # ── writes ──────────────────────────────────────────────────────

    def store_embedding(self, data: dict) -> dict:
        """Upsert one embedding."""
        return self.store_embeddings_batch([data])[0]

    def store_embeddings_batch(self, items: list[dict]) -> list[dict]:
        """Upsert a batch: one statement per JSON chunk (about 1.5 MiB), one transaction when it spans chunks."""
        if not items:
            return []
        db = self._db()
        docs = [self._doc_from(it, self._resolve_ns(it.get("namespace"))) for it in items]
        # One statement cannot touch the same key twice (ON CONFLICT DO UPDATE): keep the last of each key.
        unique: dict[tuple, dict] = {}
        for d in docs:
            key = (d["namespace"], d.get("ontology_id") or "", d["entity_uri"], d["embedding_type"], d["model_id"])
            unique[key] = d
        chunks = list(bulk_json_chunks(self._row_from_doc(d) for d in unique.values()))
        if len(chunks) == 1:
            db.execute(_UPSERT, {"rows": chunks[0]})
        else:
            with db.transaction() as tx:
                for chunk in chunks:
                    tx.execute(_UPSERT, {"rows": chunk})
        return docs

    # ── reads ───────────────────────────────────────────────────────

    def get_embeddings_for_entity(
        self,
        entity_uri: str,
        entity_type: str | None = None,
        embedding_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict]:
        """Return stored embeddings for one entity, optionally filtered."""
        db = self._db()
        where = "namespace = :ns AND entity_uri = :uri"
        params: dict[str, Any] = {"ns": self._resolve_ns(namespace), "uri": entity_uri}
        if entity_type:
            where += " AND entity_type = :et"
            params["et"] = entity_type
        if embedding_type:
            where += " AND embedding_type = :emb"
            params["emb"] = embedding_type

        def build(last, limit):
            return (
                f"SELECT {_DOC_COLUMNS} FROM coa_embedding WHERE {where} AND id > :after ORDER BY id LIMIT :limit",
                {**params, "after": last["id"] if last else 0, "limit": limit},
            )

        return [self._to_doc(r) for r in db.query_paged(build, page_size=50)]

    def list_embeddings_for_ontology(
        self,
        ontology_id: str,
        entity_type: str | None = None,
        embedding_type: str | None = None,
        namespace: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return embeddings for an ontology. With ``limit`` the vectors are left out (metadata only)."""
        size = _UNLIMITED_LIST_CAP if limit is None else limit
        if size < 1:
            return []
        db = self._db()
        where = "namespace = :ns AND ontology_id = :oid"
        params: dict[str, Any] = {"ns": self._resolve_ns(namespace), "oid": ontology_id}
        if entity_type:
            where += " AND entity_type = :et"
            params["et"] = entity_type
        if embedding_type:
            where += " AND embedding_type = :emb"
            params["emb"] = embedding_type
        columns = "id, entity_uri, ontology_id, text" if limit is not None else _DOC_COLUMNS

        def build(last, page):
            return (
                f"SELECT {columns} FROM coa_embedding WHERE {where} AND id > :after ORDER BY id LIMIT :limit",
                {**params, "after": last["id"] if last else 0, "limit": min(page, size)},
            )

        rows = db.query_paged(build, page_size=min(size, 200 if limit is None else 2000), max_rows=size)
        if limit is not None:
            return [
                {
                    "entity_uri": r["entity_uri"],
                    "ontology_id": r.get("ontology_id") or None,
                    "text": r.get("text") or "",
                }
                for r in rows
            ]
        return [self._to_doc(r) for r in rows]

    def list_searchable_entity_uris(self, ontology_id: str, namespace: str | None = None) -> list[str]:
        """Return the distinct ``entity_uri`` of every embedding of the ontology (no vectors transferred)."""
        db = self._db()
        params = {"ns": self._resolve_ns(namespace), "oid": ontology_id}

        def build(last, limit):
            return (
                'SELECT DISTINCT entity_uri COLLATE "C" AS entity_uri FROM coa_embedding '
                'WHERE namespace = :ns AND ontology_id = :oid AND entity_uri COLLATE "C" > :after '
                "ORDER BY entity_uri LIMIT :limit",
                {**params, "after": last["entity_uri"] if last else "", "limit": limit},
            )

        return [r["entity_uri"] for r in db.query_paged(build, page_size=2000)]

    def search_nearest(
        self,
        vector: list[float],
        embedding_type: str,
        model_id: str,
        entity_type: str | None = None,
        ontology_id: str | None = None,
        top_k: int = 10,
        namespace: str | None = None,
    ) -> list[EmbeddingHit]:
        """k-NN by cosine distance. ``score`` is raw cosine similarity in ``[-1, 1]``."""
        if top_k < 1:
            return []
        db = self._db()
        info = self._info
        where = "namespace = :ns AND embedding_type = :emb AND model_id = :mid"
        params: dict[str, Any] = {
            "ns": self._resolve_ns(namespace),
            "emb": embedding_type,
            "mid": model_id,
            "v": vector_text(self._padded(vector)),
            "k": top_k,
        }
        filtered = False
        if entity_type:
            where += " AND entity_type = :et"
            params["et"] = entity_type
            filtered = True
        if ontology_id:
            where += " AND ontology_id = :oid"
            params["oid"] = ontology_id
            filtered = True

        def sql(with_vector: bool) -> str:
            cols = "id, entity_uri, embedding_type, model_id, ontology_id, entity_type, text, "
            if with_vector:
                cols += "embedding::text AS embedding, "
            return (
                f"SELECT * FROM (SELECT {cols}1 - (embedding <=> CAST(CAST(:v AS text) AS vector)) AS score "
                f"FROM coa_embedding WHERE {where} "
                "ORDER BY embedding <=> CAST(CAST(:v AS text) AS vector), id LIMIT :k) q ORDER BY score DESC, id"
            )

        iterative = filtered and info is not None and info.supports_iterative_scan
        ef = min(max(top_k, _DEFAULT_EF_SEARCH), _MAX_EF_SEARCH)

        def run(with_vector: bool) -> list[dict]:
            if not iterative and ef == _DEFAULT_EF_SEARCH:
                return db.execute(sql(with_vector), params)
            with db.transaction() as tx:
                tx.execute("SELECT set_config('hnsw.ef_search', :v, true)", {"v": str(ef)})
                if iterative:
                    tx.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
                return tx.execute(sql(with_vector), params)

        try:
            rows = run(True)
        except ResponseTooLargeError:
            # top_k vectors do not fit in one response: rank first, then fetch the vectors in pages.
            rows = run(False)
            vectors = self._vectors_by_id([r["id"] for r in rows])
            for r in rows:
                r["embedding"] = vectors.get(r["id"], "[]")
        return [
            EmbeddingHit(
                entity_uri=r["entity_uri"],
                embedding_type=r["embedding_type"],
                model_id=r["model_id"],
                vector=json.loads(r["embedding"]) if r.get("embedding") else [],
                ontology_id=r.get("ontology_id") or None,
                entity_type=r.get("entity_type"),
                score=float(r["score"]),
                text=r.get("text"),
            )
            for r in rows
        ]

    def _vectors_by_id(self, ids: list[int]) -> dict[int, str]:
        db = self._client
        assert db is not None
        out: dict[int, str] = {}
        step = 25
        for i in range(0, len(ids), step):
            part = ids[i : i + step]
            rows = db.execute(
                "SELECT id, embedding::text AS embedding FROM coa_embedding "
                "WHERE id IN (SELECT CAST(x AS bigint) FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS x)",
                {"ids": json_text(part)},
            )
            out.update({r["id"]: r["embedding"] for r in rows})
        return out

    # ── deletes ─────────────────────────────────────────────────────

    def _delete_batches(self, where: str, params: Mapping[str, Any]) -> int:
        """Delete matching rows in bounded batches (the Data API stops statements at 45 seconds)."""
        db = self._db()
        total = 0
        while True:
            rows = db.execute(
                "WITH d AS (DELETE FROM coa_embedding WHERE id IN "
                f"(SELECT id FROM coa_embedding WHERE {where} LIMIT :batch) RETURNING 1) "
                "SELECT count(*) AS n FROM d",
                {**params, "batch": _DELETE_BATCH},
            )
            n = int(rows[0]["n"]) if rows else 0
            total += n
            if n < _DELETE_BATCH:
                return total

    def delete_embeddings_for_ontology(self, ontology_id: str, namespace: str | None = None) -> int:
        """Delete every embedding of an ontology; returns the deleted count."""
        return self._delete_batches(
            "namespace = :ns AND ontology_id = :oid",
            {"ns": self._resolve_ns(namespace), "oid": ontology_id},
        )

    def delete_embeddings_for_entities(
        self,
        entity_uris: Iterable[str],
        ontology_id: str,
        namespace: str | None = None,
    ) -> int:
        """Delete the embeddings of the given entities within one ontology; other ontologies keep theirs."""
        uris = [u for u in dict.fromkeys(entity_uris) if u]
        if not uris or not ontology_id:
            return 0
        ns = self._resolve_ns(namespace)
        total = 0
        for i in range(0, len(uris), _ENTITY_CHUNK):
            total += self._delete_batches(
                "namespace = :ns AND ontology_id = :oid AND entity_uri IN "
                "(SELECT jsonb_array_elements_text(CAST(:uris AS jsonb)))",
                {"ns": ns, "oid": ontology_id, "uris": json_text(uris[i : i + _ENTITY_CHUNK])},
            )
        return total

    def delete_index(self, namespace: str | None = None) -> bool:
        """Delete every embedding of a namespace. Returns True when there was any."""
        return self._delete_batches("namespace = :ns", {"ns": self._resolve_ns(namespace)}) > 0

    # ── health ──────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Probe the database with ``SELECT 1``."""
        try:
            if self._client is None:
                self._client = default_client()
            self._client.execute("SELECT 1 AS ok")
            return {"status": "ok"}
        except AuroraConfigError:
            return {"status": "unconfigured"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
