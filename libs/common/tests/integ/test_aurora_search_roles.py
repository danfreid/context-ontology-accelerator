# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in integration test of the least-privilege search roles against a real local Postgres.

Skipped unless ``COA_PG_TEST_DSN`` is set and psycopg is importable. The DSN must belong to the
table owner / admin user (it applies ``aurora-init.sql``, which creates ``coa_search_rw`` and
``coa_search_ro``, and builds the HNSW index as the owner, the way the migrator does). The tests
then connect as each role:

    COA_PG_TEST_DSN=postgresql://admin:pw@localhost:5432/postgres pytest -m integ libs/common/tests/integ
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from coa_common.aurora_search.index import AuroraSearchIndex, SearchDoc

pytestmark = pytest.mark.integ

DSN = os.environ.get("COA_PG_TEST_DSN")
if not DSN:
    pytest.skip("COA_PG_TEST_DSN is not set", allow_module_level=True)
psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed")
pytest.importorskip("psycopg.rows")

from test_aurora_search_pg import PgDataApi  # noqa: E402  (sibling integ module; same stand-in client)

INIT_SQL = Path(__file__).resolve().parents[4] / "infra/lib/stacks/foundation/migrations/aurora-init.sql"
SCOPE = "coa-roles-test-scope"
DIMENSIONS = 3
ROLE_PASSWORD = "coa-roles-test-pw"  # noqa: S105  throwaway local test database
SEARCH_TABLES = ("search_doc", "search_corpus_stats", "search_term_stats", "entity_label")


def _init_sql() -> str:
    """aurora-init.sql with only the pgrouting extension line removed (the test image has no pgrouting)."""
    lines = INIT_SQL.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(r"\s*CREATE EXTENSION IF NOT EXISTS pgrouting\b", ln)]
    assert len(kept) == len(lines) - 1
    return "\n".join(kept)


def _dsn_for(role: str) -> str:
    parts = urlsplit(DSN or "")
    host = parts.hostname or "localhost"
    netloc = f"{quote(role)}:{quote(ROLE_PASSWORD)}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="module")
def roles_ready() -> Iterator[None]:
    """Apply the schema, build the HNSW index as owner, set role passwords, add a dummy graph table."""
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(_init_sql())
        # The migrator builds this index as the owner, so the rw role never needs CREATE INDEX.
        admin.execute(
            f"CREATE INDEX IF NOT EXISTS search_doc_embedding_hnsw_{DIMENSIONS} ON search_doc "
            f"USING hnsw (CAST(embedding AS vector({DIMENSIONS})) vector_cosine_ops) WHERE embedding IS NOT NULL"
        )
        for role in ("coa_search_rw", "coa_search_ro"):
            statement = admin.execute(
                "SELECT format('ALTER ROLE %%I PASSWORD %%L', CAST(%s AS text), CAST(%s AS text))",
                (role, ROLE_PASSWORD),
            ).fetchone()
            assert statement is not None
            admin.execute(statement[0])  # type: ignore[arg-type]
        admin.execute("DROP TABLE IF EXISTS graph_node")
        admin.execute("CREATE TABLE graph_node (id TEXT PRIMARY KEY, label TEXT)")
        admin.execute("INSERT INTO graph_node VALUES ('n1', 'secret')")
        # Seed one document as the owner so ro has something to read.
        owner_index = AuroraSearchIndex(PgDataApi(admin), DIMENSIONS)  # type: ignore[arg-type]
        owner_index.delete_scope(SCOPE)
        owner_index.upsert_docs(
            SCOPE, [SearchDoc(body="seeded owner document zirconium", entity_uri="urn:seed", embedding=[0, 1, 0])]
        )
        try:
            yield
        finally:
            owner_index.delete_scope(SCOPE)
            admin.execute("DROP TABLE IF EXISTS graph_node")


@pytest.fixture
def rw_conn(roles_ready: None) -> Iterator[Any]:
    with psycopg.connect(_dsn_for("coa_search_rw")) as conn:
        yield conn


@pytest.fixture
def ro_conn(roles_ready: None) -> Iterator[Any]:
    with psycopg.connect(_dsn_for("coa_search_ro")) as conn:
        yield conn


def _denied(conn: Any, sql: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql)
    conn.rollback()


def test_rw_upserts_document_with_embedding_without_creating_index(rw_conn: Any) -> None:
    index = AuroraSearchIndex(PgDataApi(rw_conn), DIMENSIONS)  # type: ignore[arg-type]
    index.delete_scope(SCOPE + "-rw")
    assert index.upsert_docs(
        SCOPE + "-rw", [SearchDoc(body="rw embedded document hafnium", entity_uri="urn:rw", embedding=[1, 0, 0])]
    )
    hits = index.vector_search(SCOPE + "-rw", [1, 0, 0], top_k=3)
    assert [h.key for h in hits] == ["urn:rw"]
    assert index.delete_scope(SCOPE + "-rw") == 1


def test_rw_keyword_search_and_delete_scope(rw_conn: Any) -> None:
    index = AuroraSearchIndex(PgDataApi(rw_conn), DIMENSIONS)  # type: ignore[arg-type]
    scope = SCOPE + "-rw-kw"
    index.upsert_docs(scope, [SearchDoc(body="niobium alloy shipment", entity_uri="urn:nb", embedding=[0, 0, 1])])
    assert [h.key for h in index.keyword_search(scope, "niobium", top_k=3)] == ["urn:nb"]
    assert index.delete_scope(scope) == 1
    assert index.keyword_search(scope, "niobium", top_k=3) == []


def test_ro_can_search(ro_conn: Any) -> None:
    index = AuroraSearchIndex(PgDataApi(ro_conn), DIMENSIONS)  # type: ignore[arg-type]
    assert [h.key for h in index.keyword_search(SCOPE, "zirconium", top_k=3)] == ["urn:seed"]
    assert [h.key for h in index.vector_search(SCOPE, [0, 1, 0], top_k=3)] == ["urn:seed"]


def test_ro_writes_are_denied(ro_conn: Any) -> None:
    index = AuroraSearchIndex(PgDataApi(ro_conn), DIMENSIONS)  # type: ignore[arg-type]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        index.upsert_docs(SCOPE, [SearchDoc(body="ro must not write", entity_uri="urn:ro", embedding=[1, 1, 0])])
    ro_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        index.delete_scope(SCOPE)
    ro_conn.rollback()
    for table in SEARCH_TABLES:
        _denied(ro_conn, f"DELETE FROM {table}")
    # The seeded document is untouched.
    assert [h.key for h in index.keyword_search(SCOPE, "zirconium", top_k=3)] == ["urn:seed"]


@pytest.mark.parametrize("role_conn", ["rw_conn", "ro_conn"])
def test_roles_cannot_touch_tables_outside_search(role_conn: str, request: pytest.FixtureRequest) -> None:
    conn = request.getfixturevalue(role_conn)
    _denied(conn, "SELECT * FROM graph_node")
    _denied(conn, "INSERT INTO graph_node VALUES ('n2', 'x')")
    _denied(conn, "UPDATE graph_node SET label = 'x'")
    _denied(conn, "DELETE FROM graph_node")
