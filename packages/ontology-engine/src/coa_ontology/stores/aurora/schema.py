# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""App-owned DDL for the Aurora backend.

``aurora-init.sql`` (infra) creates the extensions and the ``ontology_engine``
DynamoDB-mirror table but has no table for triples or ontology embeddings, so
the engine creates its own ``coa_*`` tables. The DDL lives in Python strings
because the package cannot ship ``.sql`` data files.

:func:`ensure_schema` runs once per client. When the schema is current it costs
one round trip (version, pgvector version and embedding dimension in a single
statement). Otherwise it applies the idempotent statements below in one
transaction under an advisory lock, so concurrent cold starts do not race.

The only value interpolated into DDL is the vector dimension, which is coerced
with ``int()`` and range checked; everything else is a constant.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from dataclasses import dataclass

from botocore.exceptions import ClientError
from coa_common.aurora_data_api import DataApiClient

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
_ADVISORY_LOCK_KEY = 7_272_740_001
_MAX_HNSW_DIMENSIONS = 2000


def configured_dimensions() -> int:
    """Embedding dimension: ``AURORA_VECTOR_DIMENSIONS``, else ``OSS_DIMENSIONS``, else 1024."""
    raw = os.getenv("AURORA_VECTOR_DIMENSIONS") or os.getenv("OSS_DIMENSIONS") or "1024"
    dims = int(raw)
    if not 1 <= dims <= _MAX_HNSW_DIMENSIONS:
        raise ValueError(f"Vector dimensions must be between 1 and {_MAX_HNSW_DIMENSIONS} (HNSW limit), got {dims}")
    return dims


def ddl_statements(dimensions: int) -> list[str]:
    """Idempotent DDL, in dependency order, for a vector column of ``dimensions``."""
    dims = int(dimensions)
    if not 1 <= dims <= _MAX_HNSW_DIMENSIONS:
        raise ValueError(f"Vector dimensions must be between 1 and {_MAX_HNSW_DIMENSIONS}, got {dims}")
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE TABLE IF NOT EXISTS coa_schema_meta (key text PRIMARY KEY, value text NOT NULL)",
        # The DynamoDB single-table mirror from aurora-init.sql, unchanged. Created only for databases
        # that were not initialised by the stack (local development, tests).
        """CREATE TABLE IF NOT EXISTS ontology_engine (
            pk text NOT NULL,
            sk text NOT NULL,
            attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (pk, sk)
        )""",
        # begins_with(PK, :prefix) scans in dynamo_store: the default collation cannot serve LIKE 'prefix%'.
        "CREATE INDEX IF NOT EXISTS ontology_engine_pk_prefix ON ontology_engine (pk text_pattern_ops)",
        # One row per RDF quad. Terms are stored flat: o_kind is iri | bnode | literal.
        # md5(o) keeps long literals out of the btree key (about 2.7 KB limit) and still gives set semantics.
        """CREATE TABLE IF NOT EXISTS coa_triple (
            id bigserial PRIMARY KEY,
            namespace text NOT NULL,
            graph_uri text NOT NULL,
            s text NOT NULL,
            s_kind text NOT NULL DEFAULT 'iri',
            p text NOT NULL,
            o text NOT NULL,
            o_kind text NOT NULL,
            o_lang text NOT NULL DEFAULT '',
            o_datatype text NOT NULL DEFAULT ''
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS coa_triple_uq "
        "ON coa_triple (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype)",
        # Traversal: covering indexes over IRI-to-IRI edges, namespace first because every read is namespace scoped.
        "CREATE INDEX IF NOT EXISTS coa_triple_fwd "
        "ON coa_triple (namespace, s, p) INCLUDE (o, graph_uri) WHERE o_kind = 'iri'",
        "CREATE INDEX IF NOT EXISTS coa_triple_rev "
        "ON coa_triple (namespace, o, p) INCLUDE (s, graph_uri) WHERE o_kind = 'iri'",
        "CREATE INDEX IF NOT EXISTS coa_triple_ns_p ON coa_triple (namespace, p, s)",
        "CREATE INDEX IF NOT EXISTS coa_triple_label_trgm "
        "ON coa_triple USING gin (lower(o) gin_trgm_ops) WHERE o_kind = 'literal'",
        f"""CREATE TABLE IF NOT EXISTS coa_embedding (
            id bigserial PRIMARY KEY,
            namespace text NOT NULL,
            ontology_id text NOT NULL DEFAULT '',
            entity_uri text NOT NULL,
            entity_type text,
            embedding_type text NOT NULL,
            model_id text NOT NULL,
            text text NOT NULL DEFAULT '',
            context_text text,
            data_source_id text,
            query_backend text,
            embedding vector({dims}) NOT NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS coa_embedding_uq "
        "ON coa_embedding (namespace, ontology_id, entity_uri, embedding_type, model_id)",
        "CREATE INDEX IF NOT EXISTS coa_embedding_entity ON coa_embedding (namespace, entity_uri)",
        "CREATE INDEX IF NOT EXISTS coa_embedding_hnsw ON coa_embedding USING hnsw (embedding vector_cosine_ops)",
    ]


@dataclass(frozen=True)
class SchemaInfo:
    """What :func:`ensure_schema` learned about the database."""

    version: str
    dimensions: int
    pgvector_version: str

    @property
    def supports_iterative_scan(self) -> bool:
        """True for pgvector >= 0.8, which has ``hnsw.iterative_scan``."""
        try:
            major, minor = (int(x) for x in self.pgvector_version.split(".")[:2])
        except ValueError:
            return False
        return (major, minor) >= (0, 8)


_cache: weakref.WeakKeyDictionary[DataApiClient, SchemaInfo] = weakref.WeakKeyDictionary()
_lock = threading.Lock()

_READ_STATE = """
SELECT
  (SELECT value FROM coa_schema_meta WHERE key = 'version') AS version,
  (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector,
  (SELECT atttypmod FROM pg_attribute
     WHERE attrelid = 'coa_embedding'::regclass AND attname = 'embedding') AS dimensions
"""

_READ_DIMENSIONS = """
SELECT
  (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector,
  (SELECT atttypmod FROM pg_attribute
     WHERE attrelid = 'coa_embedding'::regclass AND attname = 'embedding') AS dimensions
"""


_default_client: DataApiClient | None = None


def default_client() -> DataApiClient:
    """The process-wide Data API client (lazy: no env, SSM or AWS call until the first statement)."""
    global _default_client
    with _lock:
        if _default_client is None:
            _default_client = DataApiClient()
        return _default_client


def reset_schema_cache() -> None:
    """Forget which clients were verified (tests that drop the schema)."""
    with _lock:
        _cache.clear()


def _is_missing_relation(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    message = str(exc.response.get("Error", {}).get("Message", "")).lower()
    return "does not exist" in message and ("relation" in message or "coa_" in message)


def _check_dimensions(found: int | None, wanted: int) -> None:
    if found is not None and int(found) != wanted:
        raise RuntimeError(
            f"coa_embedding.embedding is vector({found}) but AURORA_VECTOR_DIMENSIONS/OSS_DIMENSIONS is {wanted}. "
            "Change the setting back, or migrate the table to the new dimension (it cannot be altered in place)."
        )


def ensure_schema(client: DataApiClient, dimensions: int | None = None) -> SchemaInfo:
    """Create or verify the ``coa_*`` tables once per client and return what was found.

    Raises:
        RuntimeError: The existing embedding column has a different dimension.
    """
    wanted = dimensions or configured_dimensions()
    with _lock:
        cached = _cache.get(client)
    if cached is not None:
        _check_dimensions(cached.dimensions, wanted)
        return cached

    state: dict | None = None
    try:
        rows = client.execute(_READ_STATE)
        state = rows[0] if rows else None
    except ClientError as exc:
        if not _is_missing_relation(exc):
            raise
    if state and state.get("version") == SCHEMA_VERSION:
        _check_dimensions(state.get("dimensions"), wanted)
        info = SchemaInfo(SCHEMA_VERSION, int(state["dimensions"]), str(state.get("pgvector") or "0"))
    else:
        info = _apply(client, wanted)
    with _lock:
        _cache[client] = info
    return info


def _apply(client: DataApiClient, dimensions: int) -> SchemaInfo:
    log.info("Applying Aurora ontology schema version %s", SCHEMA_VERSION)
    with client.transaction() as tx:
        tx.execute("SELECT pg_advisory_xact_lock(:k)", {"k": _ADVISORY_LOCK_KEY})
        for statement in ddl_statements(dimensions):
            tx.execute(statement)
        tx.execute(
            "INSERT INTO coa_schema_meta (key, value) VALUES ('version', :v) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"v": SCHEMA_VERSION},
        )
        rows = tx.execute(_READ_DIMENSIONS)
    found = rows[0] if rows else {}
    _check_dimensions(found.get("dimensions"), dimensions)
    return SchemaInfo(SCHEMA_VERSION, dimensions, str(found.get("pgvector") or "0"))
