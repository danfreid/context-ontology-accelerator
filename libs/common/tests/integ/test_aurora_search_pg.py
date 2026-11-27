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
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from coa_common.aurora_data_api import AuroraConfig, AuroraDataApiClient
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


def _init_sql() -> str:
    """aurora-init.sql with only the pgrouting extension line removed (the test image has no pgrouting)."""
    lines = INIT_SQL.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not re.match(r"\s*CREATE EXTENSION IF NOT EXISTS pgrouting\b", ln)]
    assert len(kept) == len(lines) - 1
    return "\n".join(kept)


class PgDataApi(AuroraDataApiClient):
    """The shared AuroraDataApiClient with its rds-data calls replaced by psycopg.

    Everything above ``_call`` (parameter binding, transactions, batching, keyset paging, JSON
    results) is the production code. Database errors propagate as psycopg errors.
    """

    def __init__(self, conn: Any) -> None:
        super().__init__(AuroraConfig(resource_arn="local-pg", secret_arn="local-secret"))
        self.conn = conn
        self._tx_count = 0

    @staticmethod
    def _translate(sql: str, parameters: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        values: dict[str, Any] = {}
        for p in parameters:
            (kind, value), *_ = p["value"].items()
            values[p["name"]] = None if kind == "isNull" else value
        return _PLACEHOLDER.sub(lambda m: f"%({m.group(1)})s", sql.replace("%", "%%")), values

    def _run(self, sql: str, parameters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        text, values = self._translate(sql, parameters)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(text, values)
            return (list(cur.fetchall()) if cur.description else []), max(cur.rowcount, 0)

    def _call(self, op: str, kwargs: dict[str, Any], *, retry_resume: bool) -> dict[str, Any]:
        if op == "begin_transaction":
            self._tx_count += 1
            return {"transactionId": f"tx-{self._tx_count}"}
        if op == "commit_transaction":
            self.conn.commit()
            return {}
        if op == "rollback_transaction":
            self.conn.rollback()
            return {}
        in_tx = "transactionId" in kwargs
        try:
            if op == "execute_statement":
                rows, updated = self._run(kwargs["sql"], kwargs["parameters"])
                response: dict[str, Any] = {"numberOfRecordsUpdated": updated}
                if rows:
                    response["formattedRecords"] = json.dumps(rows, default=str)
            else:
                for parameters in kwargs["parameterSets"]:
                    self._run(kwargs["sql"], parameters)
                response = {}
        except BaseException:
            if not in_tx:
                self.conn.rollback()
            raise
        if not in_tx:
            self.conn.commit()  # outside a transaction the Data API commits each statement
        return response


@pytest.fixture(scope="module")
def index() -> Iterator[AuroraSearchIndex]:
    with psycopg.connect(DSN) as conn:
        conn.execute(_init_sql())
        conn.commit()
        idx = AuroraSearchIndex(PgDataApi(conn), DIMENSIONS)
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


# ── concurrency, recounts, paging, phrase recall, namespace-keyed labels ──────


@contextmanager
def _fresh_scope() -> Iterator[tuple[str, AuroraSearchIndex]]:
    scope = f"coa-test-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN) as conn:
        idx = AuroraSearchIndex(PgDataApi(conn), DIMENSIONS)
        try:
            yield scope, idx
        finally:
            idx.delete_scope(scope)
            conn.commit()


def _assert_stats_equal_recount(index: AuroraSearchIndex, scope: str) -> None:
    c = index._client
    docs = c.execute(
        "SELECT count(*) AS n, COALESCE(avg(doc_len), 0) AS a FROM search_doc WHERE namespace = :ns", {"ns": scope}
    )[0]
    stats = c.execute("SELECT doc_count, avg_doc_len FROM search_corpus_stats WHERE namespace = :ns", {"ns": scope})
    if docs["n"] == 0:
        assert not stats or stats[0]["doc_count"] == 0
    else:
        assert stats[0]["doc_count"] == docs["n"]
        assert stats[0]["avg_doc_len"] == pytest.approx(float(docs["a"]))
    expected = {
        r["term"]: r["n"]
        for r in c.execute(
            "SELECT t AS term, count(*) AS n FROM search_doc, unnest(tsvector_to_array(tsv)) AS t "
            "WHERE namespace = :ns GROUP BY t",
            {"ns": scope},
        )
    }
    actual = {
        r["term"]: r["doc_freq"]
        for r in c.execute("SELECT term, doc_freq FROM search_term_stats WHERE namespace = :ns", {"ns": scope})
    }
    assert actual == expected


def _run_threads(workers: list[Any]) -> None:
    errors: list[BaseException] = []

    def wrap(fn: Any) -> None:
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def _upsert_in_own_connection(scope: str, docs: list[SearchDoc]) -> None:
    with psycopg.connect(DSN) as conn:
        AuroraSearchIndex(PgDataApi(conn), DIMENSIONS).upsert_docs(scope, docs)  # type: ignore[arg-type]


def test_concurrent_upserts_of_same_doc_keep_stats_exact() -> None:
    with _fresh_scope() as (scope, index):
        bodies = [f"alpha beta gamma variant{i} " + "pad " * i for i in range(6)]
        _run_threads(
            [
                lambda body=body: _upsert_in_own_connection(scope, [SearchDoc(body=body, entity_uri="urn:same")])
                for body in bodies
            ]
        )
        count = index._client.execute("SELECT count(*) AS n FROM search_doc WHERE namespace = :ns", {"ns": scope})
        assert count[0]["n"] == 1
        _assert_stats_equal_recount(index, scope)


def test_concurrent_upserts_of_different_docs_keep_stats_exact() -> None:
    with _fresh_scope() as (scope, index):
        _run_threads(
            [
                lambda w=w: _upsert_in_own_connection(
                    scope,
                    [
                        SearchDoc(body=f"shared term worker{w} item{j} common", entity_uri=f"urn:w{w}-{j}")
                        for j in range(10)
                    ],
                )
                for w in range(5)
            ]
        )
        count = index._client.execute("SELECT count(*) AS n FROM search_doc WHERE namespace = :ns", {"ns": scope})
        assert count[0]["n"] == 50
        _assert_stats_equal_recount(index, scope)


def test_update_and_delete_stats_equal_recount() -> None:
    with _fresh_scope() as (scope, index):
        index.upsert_docs(
            scope,
            [SearchDoc(body=f"red green blue doc{i} " + "word " * i, entity_uri=f"urn:d{i}") for i in range(8)],
        )
        _assert_stats_equal_recount(index, scope)
        index.upsert_docs(scope, [SearchDoc(body="purple orange only", entity_uri="urn:d1")])
        index.upsert_docs(scope, [SearchDoc(body="red red red " + "long " * 30, entity_uri="urn:d2")])
        _assert_stats_equal_recount(index, scope)
        assert index.delete_docs(scope, filter_metadata={"entity_uri": "urn:d3"}) == 1
        assert index.delete_docs(scope, filter_metadata={"entity_uri": "urn:missing"}) == 0
        _assert_stats_equal_recount(index, scope)


def test_iter_pages_over_many_rows_with_small_pages() -> None:
    with _fresh_scope() as (scope, index):
        index.upsert_docs(scope, [SearchDoc(body=f"paged row {i}", entity_uri=f"urn:p{i:02d}") for i in range(25)])

        def build(last: Any, limit: int) -> tuple[str, dict[str, Any]]:
            after = " AND doc_id > :after" if last else ""
            params = {"ns": scope, "lim": limit, **({"after": last["doc_id"]} if last else {})}
            return f"SELECT doc_id FROM search_doc WHERE namespace = :ns{after} ORDER BY doc_id LIMIT :lim", params

        pages = list(index._client.iter_pages(build, page_size=7))  # type: ignore[attr-defined]
        assert [len(p) for p in pages] == [7, 7, 7, 4]
        ids = [r["doc_id"] for p in pages for r in p]
        assert len(ids) == len(set(ids)) == 25


def test_phrase_recall_beyond_cap_returns_best_ranked() -> None:
    with _fresh_scope() as (scope, index):
        index.upsert_docs(
            scope,
            [
                SearchDoc(body=("letter of credit " * n) + "filler " * (7 - n), entity_uri=f"urn:lc{n}")
                for n in range(1, 7)
            ],
        )
        hits = index.keyword_search(scope, "", top_k=3, recall=3, phrases=["letter of credit"])
        assert {h.key for h in hits} == {"urn:lc4", "urn:lc5", "urn:lc6"}
        assert all(h.matched_phrases for h in hits)


def test_fuzzy_labels_are_namespace_keyed_and_rename_removes_old_label() -> None:
    with _fresh_scope() as (ns_a, index), _fresh_scope() as (ns_b, _other):
        index.upsert_docs(ns_a, [SearchDoc(body="trader", entity_uri="urn:shared", label="Glencore")])
        index.upsert_docs(ns_b, [SearchDoc(body="trader", entity_uri="urn:shared", label="Glencore International")])
        assert {h.label for h in index.fuzzy_entities(ns_a, "Glencore")} == {"Glencore"}
        assert {h.label for h in index.fuzzy_entities(ns_b, "Glencore")} == {"Glencore International"}
        index.upsert_docs(ns_a, [SearchDoc(body="trader", entity_uri="urn:shared", label="Vitol")])
        assert index.fuzzy_entities(ns_a, "Glencore") == []
        assert [h.label for h in index.fuzzy_entities(ns_a, "Vitol")] == ["Vitol"]
        assert {h.label for h in index.fuzzy_entities(ns_b, "Glencore")} == {"Glencore International"}
