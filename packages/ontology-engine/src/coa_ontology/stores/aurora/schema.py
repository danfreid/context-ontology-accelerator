# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema check for the Aurora graph and metadata stores.

``infra/lib/stacks/foundation/migrations/aurora-init.sql`` owns every table (``coa_triple``,
``ontology_engine``, ``search_doc`` and friends) and runs through the aurora-migrator custom resource. At runtime
the engine role (``coa_engine_rw``) only reads and writes rows, so :func:`check_schema` is read-only: it
confirms the tables exist and that the stored vector width matches the configured dimension, and raises a
clear error naming ``aurora-init.sql`` when it does not.

:func:`apply_schema_for_dev` holds the DDL for the two engine tables. It is for local development and tests
against a bare database; neither store calls it at runtime.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass

from coa_common.aurora_data_api import AuroraDataApiClient, AuroraQueryError, build_client_from_env

log = logging.getLogger(__name__)

INIT_SQL = "infra/lib/stacks/foundation/migrations/aurora-init.sql"
REQUIRED_TABLES = ("coa_triple", "ontology_engine")
_ADVISORY_LOCK_KEY = 7_272_740_001
_MAX_HNSW_DIMENSIONS = 2000


class AuroraSchemaError(RuntimeError):
    """The database does not have the schema ``aurora-init.sql`` creates, or it does not match the configuration."""


@dataclass(frozen=True)
class SchemaInfo:
    """What :func:`check_schema` verified."""

    dimensions: int
    stored_dimensions: int | None = None


_cache: weakref.WeakKeyDictionary[AuroraDataApiClient, SchemaInfo] = weakref.WeakKeyDictionary()
_lock = threading.Lock()
_default_client: AuroraDataApiClient | None = None

_READ_TABLES = """
SELECT
  to_regclass('coa_triple') IS NOT NULL AS has_triple,
  to_regclass('ontology_engine') IS NOT NULL AS has_engine,
  to_regclass('search_doc') IS NOT NULL AS has_search_doc
"""

_READ_STORED_DIMENSIONS = (
    "SELECT vector_dims(embedding) AS dimensions FROM search_doc WHERE embedding IS NOT NULL LIMIT 1"
)


def default_client() -> AuroraDataApiClient:
    """The process-wide Data API client (lazy: no env, SSM or AWS call until the first statement)."""
    global _default_client
    with _lock:
        if _default_client is None:
            _default_client = build_client_from_env()
        return _default_client


def reset_schema_cache() -> None:
    """Forget which clients were verified (tests that change the schema)."""
    with _lock:
        _cache.clear()


def _missing(what: str) -> AuroraSchemaError:
    return AuroraSchemaError(
        f"Aurora schema is incomplete: {what}. The schema is owned by {INIT_SQL}; "
        "run the aurora-migrator (deploy the foundation stack) before starting the engine."
    )


def check_schema(client: AuroraDataApiClient, dimensions: int | None = None) -> SchemaInfo:
    """Verify, once per client, that the engine tables exist and the vector width matches the configuration.

    Read-only. ``dimensions`` defaults to ``client.config.dimensions``.

    Raises:
        AuroraSchemaError: A table is missing, or ``search_doc`` already holds vectors of another width.
    """
    with _lock:
        cached = _cache.get(client)
    if cached is not None:
        return cached

    wanted = int(dimensions or client.config.dimensions)
    try:
        rows = client.execute(_READ_TABLES)
    except AuroraQueryError as exc:
        raise _missing(f"could not inspect the database ({exc})") from exc
    state = rows[0] if rows else {}
    absent = [
        name
        for name, key in (
            ("coa_triple", "has_triple"),
            ("ontology_engine", "has_engine"),
            ("search_doc", "has_search_doc"),
        )
        if not state.get(key)
    ]
    if absent:
        raise _missing(f"missing table(s) {', '.join(absent)}")

    found = client.execute(_READ_STORED_DIMENSIONS)
    stored = int(found[0]["dimensions"]) if found and found[0].get("dimensions") is not None else None
    if stored is not None and stored != wanted:
        raise AuroraSchemaError(
            f"search_doc holds {stored}-dimension vectors but AURORA_VECTOR_DIMENSIONS/OSS_DIMENSIONS is {wanted}. "
            "Change the setting back, or re-embed into a new index (the width cannot change in place)."
        )
    info = SchemaInfo(dimensions=wanted, stored_dimensions=stored)
    with _lock:
        _cache[client] = info
    return info


def ddl_statements() -> list[str]:
    """Idempotent DDL for the engine tables, in dependency order (the same objects as ``aurora-init.sql``)."""
    return [
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
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
    ]


def apply_schema_for_dev(client: AuroraDataApiClient) -> None:
    """Create the engine tables on a bare database. Development and tests only; never called at runtime."""
    log.info("Applying the Aurora engine schema for development")
    with client.transaction() as tx:
        tx.execute("SELECT pg_advisory_xact_lock(:k)", {"k": _ADVISORY_LOCK_KEY})
        for statement in ddl_statements():
            tx.execute(statement)
    reset_schema_cache()
