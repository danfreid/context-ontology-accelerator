# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in integration test against a real local Postgres (pgvector + pg_trgm).

Skipped unless ``COA_PG_TEST_DSN`` is set and psycopg is importable. Run with a
database that allows ``CREATE EXTENSION vector`` and ``pg_trgm``:

    COA_PG_TEST_DSN=postgresql://user:pw@localhost:5432/postgres pytest -m integ libs/common/tests/integ
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from coa_common.aurora_search.index import AuroraSearchIndex, SearchDoc

pytestmark = pytest.mark.integ

DSN = os.environ.get("COA_PG_TEST_DSN")
if not DSN:
    pytest.skip("COA_PG_TEST_DSN is not set", allow_module_level=True)
psycopg = pytest.importorskip("psycopg", reason="psycopg is not installed")
pytest.importorskip("psycopg.rows")

INIT_SQL = Path(__file__).resolve().parents[4] / "infra/lib/stacks/foundation/migrations/aurora-init.sql"
SCOPE = "coa-test-scope"
DIMENSIONS = 3
_PLACEHOLDER = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)")


class PgDataApi:
    """Minimal stand-in for AuroraDataApiClient that runs the real SQL through psycopg."""

    cluster_arn = "local-pg"

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._depth = 0

    @staticmethod
    def _translate(sql: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        pg_params = {k: (json.dumps(v) if isinstance(v, dict | list) else v) for k, v in params.items()}
        return _PLACEHOLDER.sub(lambda m: f"%({m.group(1)})s", sql.replace("%", "%%")), pg_params

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        text, pg_params = self._translate(sql, params or {})
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(text, pg_params)
            return list(cur.fetchall()) if cur.description else []

    def batch_execute(self, sql: str, param_sets: list[dict[str, Any]]) -> int:
        for params in param_sets:
            self.execute(sql, params)
        return len(param_sets)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth:
            yield
            return
        self._depth += 1
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._depth -= 1


@pytest.fixture(scope="module")
def index() -> Iterator[AuroraSearchIndex]:
    with psycopg.connect(DSN) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
        conn.commit()
        statements = [
            s
            for s in INIT_SQL.read_text(encoding="utf-8").split(";\n")
            if re.search(r"(CREATE TABLE|CREATE INDEX|ALTER TABLE)", s)
            and "pgrouting" not in s
            and "graph_edge" not in s
        ]
        for stmt in statements:
            body = "\n".join(line for line in stmt.splitlines() if not line.lstrip().startswith("--"))
            if body.strip():
                conn.execute(body)
        conn.commit()
        idx = AuroraSearchIndex(PgDataApi(conn), DIMENSIONS)  # type: ignore[arg-type]
        idx.delete_scope(SCOPE)
        idx.upsert_docs(
            SCOPE,
            [
                SearchDoc(
                    body="Glencore plc commodity trading counterparty",
                    entity_uri="urn:glencore",
                    label="Glencore",
                    embedding=[1, 0, 0],
                    doc_kind="entity",
                ),
                SearchDoc(
                    body="Harbor Terminals storage agreement",
                    entity_uri="urn:harbor",
                    label="Harbor Terminals",
                    embedding=[0, 1, 0],
                    doc_kind="entity",
                ),
                SearchDoc(
                    body="copper copper copper cathode shipment schedule", entity_uri="urn:copper1", embedding=[0, 0, 1]
                ),
                SearchDoc(body="copper shipment " + "filler " * 40, entity_uri="urn:copper2", embedding=[0, 0, 1]),
            ],
        )
        yield idx
        idx.delete_scope(SCOPE)
        conn.commit()


def test_fuzzy_glencor_finds_glencore(index: AuroraSearchIndex) -> None:
    hits = index.fuzzy_entities(SCOPE, "Glencor")
    assert hits[0].entity_id == "urn:glencore"


def test_fuzzy_harbour_finds_harbor(index: AuroraSearchIndex) -> None:
    hits = index.fuzzy_entities(SCOPE, "Harbour Terminals")
    assert hits[0].entity_id == "urn:harbor"


def test_prefix_recall(index: AuroraSearchIndex) -> None:
    hits = index.keyword_search(SCOPE, "glenc", top_k=5)
    assert [h.key for h in hits] == ["urn:glencore"]


def test_bm25_prefers_dense_short_doc(index: AuroraSearchIndex) -> None:
    hits = index.keyword_search(SCOPE, "copper", top_k=5)
    assert [h.key for h in hits] == ["urn:copper1", "urn:copper2"]


def test_hybrid_finds_misspelled_entity(index: AuroraSearchIndex) -> None:
    hits = index.search(SCOPE, "Harbour", vector=[0, 1, 0], top_k=3)
    assert hits[0].key == "urn:harbor"


def test_stats_consistent_after_delete(index: AuroraSearchIndex) -> None:
    c = index._client
    before = c.execute("SELECT doc_count FROM search_corpus_stats WHERE namespace = :ns", {"ns": SCOPE})[0]["doc_count"]
    assert index.delete_docs(SCOPE, doc_ids=None, filter_metadata={"entity_uri": "urn:copper2"}) == 1
    after = c.execute("SELECT doc_count FROM search_corpus_stats WHERE namespace = :ns", {"ns": SCOPE})[0]["doc_count"]
    assert after == before - 1
    freq = c.execute("SELECT doc_freq FROM search_term_stats WHERE namespace = :ns AND term = 'filler'", {"ns": SCOPE})
    assert freq == []
    copper = c.execute(
        "SELECT doc_freq FROM search_term_stats WHERE namespace = :ns AND term = 'copper'", {"ns": SCOPE}
    )
    assert copper[0]["doc_freq"] == 1
