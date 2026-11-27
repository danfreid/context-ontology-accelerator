# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in integration test of the entity_label re-key in aurora-init.sql on a real local Postgres.

Skipped unless ``COA_PG_TEST_DSN`` is set and psycopg is importable. The DSN must belong to a
superuser (it creates and drops a throwaway database and the vector / pg_trgm / fuzzystrmatch
extensions in it). Older deployments created ``entity_label`` with PRIMARY KEY (entity_id, label);
the init SQL must re-key that table to (namespace, entity_id, label) without losing the rows that
are still there:

    COA_PG_TEST_DSN=postgresql://postgres:pw@localhost:5432/postgres pytest -m integ libs/common/tests/integ
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.integ

DSN = os.environ.get("COA_PG_TEST_DSN")
if not DSN:
    pytest.skip("COA_PG_TEST_DSN is not set", allow_module_level=True)
psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed")
pytest.importorskip("psycopg.rows")

from test_aurora_search_pg import _init_sql  # noqa: E402  (sibling integ module; same init SQL helper)

OLD_ROWS = [
    ("e1", "ns-a", "Acme", "label"),
    ("e1", "ns-a", "ACME Inc", "alias"),
    ("e2", "ns-a", "Globex", "label"),
]


def _dsn_for_database(database: str) -> str:
    parts = urlsplit(DSN or "")
    return urlunsplit((parts.scheme, parts.netloc, "/" + database, parts.query, parts.fragment))


def _primary_key_columns(conn: Any) -> list[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
        "WHERE c.conrelid = 'entity_label'::regclass AND c.contype = 'p' ORDER BY k.ord"
    ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def old_schema_db() -> Iterator[Any]:
    """A throwaway database holding the OLD two-column-key entity_label table with rows."""
    name = f"coa_rekey_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        try:
            with psycopg.connect(_dsn_for_database(name), autocommit=True) as conn:
                for extension in ("vector", "pg_trgm", "fuzzystrmatch"):
                    conn.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
                conn.execute(
                    "CREATE TABLE entity_label ("
                    " entity_id TEXT NOT NULL, namespace TEXT NOT NULL, label TEXT NOT NULL,"
                    " kind TEXT NOT NULL DEFAULT 'label', PRIMARY KEY (entity_id, label))"
                )
                for row in OLD_ROWS:
                    conn.execute(
                        "INSERT INTO entity_label (entity_id, namespace, label, kind) VALUES (%s, %s, %s, %s)", row
                    )
                yield conn
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_init_sql_rekeys_old_entity_label_and_keeps_rows(old_schema_db: Any) -> None:
    conn = old_schema_db
    assert _primary_key_columns(conn) == ["entity_id", "label"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO entity_label (entity_id, namespace, label) VALUES ('e1', 'ns-b', 'Acme')")

    conn.execute(_init_sql())

    assert _primary_key_columns(conn) == ["namespace", "entity_id", "label"]
    # The same (entity_id, label) can now live in another namespace.
    conn.execute("INSERT INTO entity_label (entity_id, namespace, label) VALUES ('e1', 'ns-b', 'Acme')")
    assert conn.execute("SELECT count(*) FROM entity_label WHERE entity_id = 'e1' AND label = 'Acme'").fetchone() == (
        2,
    )
    # Inside one namespace the key still rejects a duplicate.
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO entity_label (entity_id, namespace, label) VALUES ('e1', 'ns-b', 'Acme')")


def test_init_sql_is_a_no_op_when_applied_again(old_schema_db: Any) -> None:
    conn = old_schema_db
    conn.execute(_init_sql())
    before = conn.execute("SELECT entity_id, namespace, label, kind FROM entity_label ORDER BY 1, 2, 3").fetchall()
    conn.execute(_init_sql())
    after = conn.execute("SELECT entity_id, namespace, label, kind FROM entity_label ORDER BY 1, 2, 3").fetchall()
    assert before == after
    assert _primary_key_columns(conn) == ["namespace", "entity_id", "label"]
    assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'coa_set_role_password'").fetchone() == (1,)
