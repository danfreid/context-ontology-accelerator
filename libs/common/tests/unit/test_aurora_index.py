# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AuroraSearchIndex with a recording fake Data API client."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from coa_common.aurora_data_api import AuroraQueryError
from coa_common.aurora_search import index as index_mod
from coa_common.aurora_search.fusion import SearchMode
from coa_common.aurora_search.index import AuroraSearchIndex, SearchDoc, doc_id_for

pytestmark = pytest.mark.unit

ADVERSARIAL = "x'; DROP TABLE search_doc; -- :* \" \\ ) | & !"
PLACEHOLDER = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)")

Handler = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class FakeTx:
    """Stand-in for `coa_common.aurora_data_api.Transaction`: statements go through the fake client."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._client.execute(sql, params, tx=self)


class FakeClient:
    cluster_arn = "arn:test"

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or (lambda sql, params: [])
        self.events: list[tuple[str, str, Any]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None, *, tx: Any = None) -> list[dict[str, Any]]:
        self.events.append(("execute", sql, params or {}))
        return self.handler(sql, params or {})

    def batch_execute(self, sql: str, param_sets: list[dict[str, Any]], *, tx: Any = None) -> int:
        self.events.append(("batch", sql, param_sets))
        return len(param_sets)

    @contextmanager
    def transaction(self) -> Iterator[FakeTx]:
        self.events.append(("tx", "BEGIN", None))
        yield FakeTx(self)
        self.events.append(("tx", "COMMIT", None))

    def sqls(self) -> list[str]:
        return [sql for kind, sql, _ in self.events if kind in ("execute", "batch")]

    def find(self, needle: str, kind: str | None = None) -> list[tuple[str, str, Any]]:
        return [e for e in self.events if needle in e[1] and (kind is None or e[0] == kind)]


@pytest.fixture(autouse=True)
def clear_ensured() -> None:
    index_mod._ENSURED_INDEXES.clear()


def make(handler: Handler | None = None, dimensions: int = 3) -> tuple[AuroraSearchIndex, FakeClient]:
    fake = FakeClient(handler)
    return AuroraSearchIndex(fake, dimensions), fake  # type: ignore[arg-type]


def assert_only_placeholders(fake: FakeClient, forbidden: list[str]) -> None:
    for kind, sql, payload in fake.events:
        if kind not in ("execute", "batch"):
            continue
        for bad in forbidden:
            assert bad not in sql, sql
        names = set(PLACEHOLDER.findall(sql))
        sets = payload if kind == "batch" else [payload]
        for params in sets:
            assert names <= set(params), (sql, names, set(params))


# ── ensure_vector_index ───────────────────────────────────────────────────


def test_ensure_vector_index_runs_once_per_process() -> None:
    idx, fake = make(dimensions=3)
    idx.ensure_vector_index()
    idx.ensure_vector_index()
    other, _ = make(dimensions=3)
    other._client = fake  # same cluster and dimension
    other.ensure_vector_index()
    creates = fake.find("CREATE INDEX")
    assert len(creates) == 1
    sql = creates[0][1]
    assert "search_doc_embedding_hnsw_3" in sql
    assert "USING hnsw" in sql
    assert "vector(3)" in sql
    assert "vector_cosine_ops" in sql
    assert "WHERE embedding IS NOT NULL" in sql


def test_ensure_vector_index_per_dimension() -> None:
    idx3, fake = make(dimensions=3)
    idx4 = AuroraSearchIndex(fake, 4)  # type: ignore[arg-type]
    idx3.ensure_vector_index()
    idx4.ensure_vector_index()
    assert len(fake.find("CREATE INDEX")) == 2


@pytest.mark.parametrize("bad", [0, -1, 16001, "1024", 1.5, True, None])
def test_dimensions_validated(bad: Any) -> None:
    with pytest.raises(ValueError):
        AuroraSearchIndex(FakeClient(), bad)  # type: ignore[arg-type]


def test_ensure_vector_index_revalidates_dimension() -> None:
    idx, fake = make()
    idx._dimensions = "3); DROP TABLE search_doc; --"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        idx.ensure_vector_index()
    assert fake.events == []


def test_ensure_vector_index_swallows_already_exists_race() -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise AuroraQueryError('relation "search_doc_embedding_hnsw_3" already exists')

    idx, _ = make(handler)
    idx.ensure_vector_index()


def test_ensure_vector_index_tolerates_non_owner_role() -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise AuroraQueryError("must be owner of table search_doc")

    idx, _ = make(handler)
    idx.ensure_vector_index()


def test_ensure_vector_index_reraises_other_errors() -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise AuroraQueryError("connection reset")

    idx, _ = make(handler)
    with pytest.raises(AuroraQueryError):
        idx.ensure_vector_index()


# ── upsert ────────────────────────────────────────────────────────────────


def upsert_handler(existing: list[dict[str, Any]], tsv_text: str) -> Handler:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "SELECT doc_id, to_jsonb" in sql:
            return existing
        if "to_tsvector('english', e.val)" in sql:
            return [{"ord": 1, "tsv_text": tsv_text}]
        return []

    return handler


def term_sets(fake: FakeClient) -> dict[str, int]:
    batches = fake.find("INSERT INTO search_term_stats", "batch")
    return {p["term"]: p["delta"] for _, _, sets in batches for p in sets}


def corpus_params(fake: FakeClient) -> dict[str, Any]:
    return fake.find("INSERT INTO search_corpus_stats", "execute")[0][2]


def test_upsert_new_doc_adds_term_deltas_and_corpus_stats() -> None:
    idx, fake = make(upsert_handler([], "'copper':1,2 'trade':3"))
    doc = SearchDoc(
        body=ADVERSARIAL + " copper copper trade",
        entity_uri="urn:e:1",
        ontology_id="onto",
        embedding=[0.5, 1.0, 0.0],
        metadata={"data_source_id": "ds"},
        label="Glencore",
        aliases=["Glencor Intl"],
    )
    assert idx.upsert_docs("coa-onto-vec", [doc]) == 1
    assert term_sets(fake) == {"copper": 1, "trade": 1}
    assert corpus_params(fake) == {"ns": "coa-onto-vec", "dc": 1, "dl": 3}
    assert fake.find("DELETE FROM search_term_stats") == []

    insert = fake.find("INSERT INTO search_doc", "execute")[0]
    params = insert[2]
    assert params["doc_id"] == doc_id_for("coa-onto-vec", doc)
    assert params["ns"] == "coa-onto-vec"
    assert params["doc_len"] == 3
    assert params["embedding"] == "[0.5,1,0]"
    lock = fake.find("pg_advisory_xact_lock", "execute")[0]
    assert "hashtextextended(:scope, 0)" in lock[1]
    assert lock[2] == {"scope": "coa-onto-vec"}
    begin = next(i for i, e in enumerate(fake.events) if e[0] == "tx")
    assert fake.events[begin + 1][1] == lock[1]  # first statement inside the transaction
    assert params["metadata"]["entity_uri"] == "urn:e:1"
    assert params["metadata"]["ontology_id"] == "onto"
    assert params["metadata"]["data_source_id"] == "ds"
    assert "to_tsvector('english', :body)" in insert[1]
    assert "CAST(:embedding AS vector)" in insert[1]

    labels = fake.find("INSERT INTO entity_label", "batch")[0][2]
    assert {(p["label"], p["kind"], p["entity_id"]) for p in labels} == {
        ("Glencore", "label", "urn:e:1"),
        ("Glencor Intl", "alias", "urn:e:1"),
    }
    assert "ON CONFLICT (namespace, entity_id, label)" in fake.find("INSERT INTO entity_label", "batch")[0][1]
    assert len(fake.find("CREATE INDEX")) == 1
    assert_only_placeholders(fake, [ADVERSARIAL, "Glencore", "urn:e:1"])
    assert [e[1] for e in fake.events if e[0] == "tx"] == ["BEGIN", "COMMIT"]


@pytest.mark.parametrize("embedding", [[1, 2, 3, 4, 5], [1, 2]])
def test_upsert_dimension_mismatch_raises(embedding: list[float]) -> None:
    idx, fake = make(upsert_handler([], "'a':1"))
    with pytest.raises(ValueError, match="dimensions"):
        idx.upsert_docs("s", [SearchDoc(body="a", entity_uri="e", embedding=embedding)])
    assert fake.find("INSERT INTO search_doc", "execute") == []


def test_vector_search_dimension_mismatch_raises() -> None:
    idx, fake = make()
    with pytest.raises(ValueError, match="dimensions"):
        idx.vector_search("s", [1, 0], 5)
    assert fake.events == []


def test_upsert_reconciles_removed_labels() -> None:
    idx, fake = make(upsert_handler([], "'a':1"))
    idx.upsert_docs("s", [SearchDoc(body="a", entity_uri="e1", label="New Name", aliases=["Alias"])])
    stale = fake.find("DELETE FROM entity_label", "batch")[0]
    assert "namespace = :ns AND entity_id = :entity_id" in stale[1]
    assert "NOT IN (SELECT jsonb_array_elements_text(CAST(:labels AS jsonb)))" in stale[1]
    assert stale[2] == [{"ns": "s", "entity_id": "e1", "labels": ["Alias", "New Name"]}]
    assert_only_placeholders(fake, ["New Name"])


def test_upsert_without_labels_keeps_existing_labels() -> None:
    idx, fake = make(upsert_handler([], "'a':1"))
    idx.upsert_docs("s", [SearchDoc(body="a", entity_uri="e1")])
    assert fake.find("entity_label") == []


def test_upsert_without_embedding_skips_index_creation() -> None:
    idx, fake = make(upsert_handler([], "'a':1"))
    idx.upsert_docs("s", [SearchDoc(body="a", entity_uri="e")])
    assert fake.find("CREATE INDEX") == []
    assert fake.find("INSERT INTO search_doc", "execute")[0][2]["embedding"] is None


def test_upsert_existing_doc_applies_old_minus_new_deltas() -> None:
    doc = SearchDoc(body="copper new", entity_uri="e")
    existing = [{"doc_id": doc_id_for("s", doc), "terms": json.dumps(["copper", "old"]), "doc_len": 5}]
    idx, fake = make(upsert_handler(existing, "'copper':1 'new':2"))
    idx.upsert_docs("s", [doc])
    assert term_sets(fake) == {"new": 1, "old": -1}
    assert corpus_params(fake) == {"ns": "s", "dc": 0, "dl": -3}
    assert len(fake.find("DELETE FROM search_term_stats WHERE namespace = :ns AND doc_freq <= 0")) == 1


def test_upsert_is_idempotent_for_identical_content() -> None:
    doc = SearchDoc(body="copper", entity_uri="e")
    existing = [{"doc_id": doc_id_for("s", doc), "terms": ["copper"], "doc_len": 1}]
    idx, fake = make(upsert_handler(existing, "'copper':1"))
    idx.upsert_docs("s", [doc])
    assert term_sets(fake) == {}
    assert fake.find("INSERT INTO search_corpus_stats") == []


def test_upsert_dedupes_same_doc_and_groups_by_50() -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "to_tsvector('english', e.val)" in sql:
            return [{"ord": i + 1, "tsv_text": "'a':1"} for i in range(len(params["bodies"]))]
        return []

    idx, fake = make(handler)
    docs = [SearchDoc(body="a", entity_uri=f"e{i}") for i in range(120)] + [SearchDoc(body="b", entity_uri="e0")]
    assert idx.upsert_docs("s", docs) == 120
    assert len([e for e in fake.events if e[0] == "tx" and e[1] == "BEGIN"]) == 3
    assert len(fake.find("INSERT INTO search_doc", "execute")) == 120
    assert idx.upsert_docs("s", []) == 0


def test_doc_id_is_stable_and_scope_sensitive() -> None:
    doc = SearchDoc(body="x", entity_uri="e", embedding_type="t", model_id="m", ontology_id="o")
    assert doc_id_for("a", doc) == doc_id_for(
        "a", SearchDoc(body="other", entity_uri="e", embedding_type="t", model_id="m", ontology_id="o")
    )
    assert doc_id_for("a", doc) != doc_id_for("b", doc)
    assert len(doc_id_for("a", doc)) == 64


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_docs_applies_negative_deltas_and_removes_zero_rows() -> None:
    calls = {"n": 0}

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "DELETE FROM search_doc WHERE doc_id IN" in sql:
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    {"doc_id": "d1", "terms": ["a", "b"], "doc_len": 4, "entity_key": "e1"},
                    {"doc_id": "d2", "terms": json.dumps(["a"]), "doc_len": 2, "entity_key": "e2"},
                ]
        return []

    idx, fake = make(handler)
    assert idx.delete_docs("s", filter_metadata={"ontology_id": ADVERSARIAL}) == 2
    assert term_sets(fake) == {"a": -2, "b": -1}
    assert corpus_params(fake) == {"ns": "s", "dc": -2, "dl": -6}
    assert len(fake.find("DELETE FROM search_term_stats WHERE namespace = :ns AND doc_freq <= 0")) == 1
    deletes = fake.find("DELETE FROM search_doc", "execute")
    assert len(deletes) == 2  # one batch with rows, one empty batch ending the loop
    assert "RETURNING doc_id" in deletes[0][1]
    assert len(fake.find("pg_advisory_xact_lock", "execute")) == 2
    label_delete = fake.find("DELETE FROM entity_label", "execute")[0]
    assert label_delete[2]["eids"] == ["e1", "e2"]
    select = deletes[0]
    assert "metadata @> CAST(:fm AS jsonb)" in select[1]
    assert select[2]["fm"] == {"ontology_id": ADVERSARIAL}
    assert_only_placeholders(fake, [ADVERSARIAL])


def test_delete_docs_applies_deltas_only_for_rows_actually_deleted() -> None:
    # The statement returns one row although two ids matched earlier: a concurrent delete won the other.
    returned = iter([[{"doc_id": "d1", "terms": ["a"], "doc_len": 3, "entity_key": "e1"}], []])

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return next(returned) if "DELETE FROM search_doc WHERE doc_id IN" in sql else []

    idx, fake = make(handler)
    assert idx.delete_docs("s", doc_ids=["d1", "d2"]) == 1
    assert term_sets(fake) == {"a": -1}
    assert corpus_params(fake) == {"ns": "s", "dc": -1, "dl": -3}


def test_delete_docs_by_ids() -> None:
    idx, fake = make()
    assert idx.delete_docs("s", doc_ids=["d1"]) == 0
    select = fake.find("DELETE FROM search_doc", "execute")[0]
    assert "doc_id IN" in select[1]
    assert select[2]["dids"] == ["d1"]


def test_delete_docs_requires_a_selector() -> None:
    idx, fake = make()
    with pytest.raises(ValueError):
        idx.delete_docs("s")
    assert fake.events == []


def test_delete_scope_loops_until_empty_and_clears_stats() -> None:
    counts = iter([5000, 120, 0])

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "WITH d AS (DELETE FROM search_doc" in sql:
            return [{"n": next(counts)}]
        return []

    idx, fake = make(handler)
    assert idx.delete_scope("s") == 5120
    chunk_deletes = fake.find("WITH d AS (DELETE FROM search_doc", "execute")
    assert len(chunk_deletes) == 3
    assert "ctid IN" in chunk_deletes[0][1]
    assert chunk_deletes[0][2]["lim"] == 5000
    kinds = [e[1] for e in fake.events]
    assert kinds[0] == "BEGIN" and kinds[-1] == "COMMIT"
    assert "pg_advisory_xact_lock" in kinds[1]
    assert kinds[-4:-1] == [
        "DELETE FROM entity_label WHERE namespace = :ns",
        "DELETE FROM search_term_stats WHERE namespace = :ns",
        "DELETE FROM search_corpus_stats WHERE namespace = :ns",
    ]


def test_delete_scope_is_idempotent_on_empty_scope() -> None:
    idx, fake = make(lambda sql, params: [{"n": 0}] if "WITH d AS" in sql else [])
    assert idx.delete_scope("s") == 0
    assert len(fake.find("DELETE FROM search_corpus_stats")) == 1


# ── read path ─────────────────────────────────────────────────────────────


def test_vector_search_orders_by_distance_and_maps_cosine() -> None:
    rows = [{"doc_id": "d1", "doc_kind": "doc", "body": "b", "metadata": '{"entity_uri": "e1"}', "score": 0.9}]
    idx, fake = make(lambda sql, params: rows)
    hits = idx.vector_search("s", [1.0, 0.0, 0.0], 5)
    sql = fake.events[0][1]
    assert "1 - (CAST(embedding AS vector(3)) <=> CAST(:q AS vector(3)))" in sql
    assert "ORDER BY CAST(embedding AS vector(3)) <=> CAST(:q AS vector(3))" in sql
    assert "embedding IS NOT NULL" in sql
    assert [e for e in fake.events if e[0] == "tx"] == []
    assert hits[0].key == "e1"
    assert hits[0].score == 0.9
    assert hits[0].metadata == {"entity_uri": "e1"}


def test_vector_search_filters_use_containment_and_iterative_scan() -> None:
    idx, fake = make()
    idx.vector_search(
        "s",
        [1, 0, 0],
        5,
        filters={
            "ontology_ids": ["o1", ADVERSARIAL],
            "require_mapped": True,
            "entity_type": "Counterparty",
            "doc_kind": ["entity"],
        },
    )
    assert [e[0] for e in fake.events] == ["tx", "execute", "execute", "tx"]
    assert fake.events[1][1] == "SET LOCAL hnsw.iterative_scan = 'relaxed_order'"
    sql, params = fake.events[2][1], fake.events[2][2]
    assert sql.count("metadata @> CAST(") == 3
    assert " OR " in sql
    assert "jsonb_exists(metadata, 'data_source_id')" in sql
    assert "doc_kind IN" in sql
    assert {"ontology_id": "o1"} in params.values()
    assert {"ontology_id": ADVERSARIAL} in params.values()
    assert {"entity_type": "Counterparty"} in params.values()
    assert_only_placeholders(fake, [ADVERSARIAL, "Counterparty"])


def test_vector_search_exclude_metadata_filter_negates_containment() -> None:
    idx, fake = make()
    idx.vector_search("s", [1, 0, 0], 5, filters={"exclude_metadata": {"query_backend": "RDF_SPARQL"}})
    sql, params = fake.events[2][1], fake.events[2][2]
    assert "NOT (metadata @> CAST(" in sql
    assert {"query_backend": "RDF_SPARQL"} in params.values()


def keyword_handler(recall: list[dict[str, Any]], stats: list[dict[str, Any]]) -> Handler:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "unnest(tsvector_to_array" in sql:
            return [{"lexeme": "copper"}, {"lexeme": "harbor"}]
        if "ts_rank" in sql:
            return recall
        if "search_corpus_stats" in sql:
            return stats
        return []

    return handler


def test_keyword_search_recalls_then_ranks_by_bm25() -> None:
    def row(doc_id: str, tsv: str, ln: int, **extra: Any) -> dict[str, Any]:
        return {
            "doc_id": doc_id,
            "doc_kind": "doc",
            "doc_len": ln,
            "tsv_text": tsv,
            "metadata": "{}",
            "body": doc_id,
            **extra,
        }

    recall = [
        row("long", "'copper':1 'x':2", 50),
        row("short", "'copper':1 'x':2", 2),
        row("none", "'zzz':1", 2),
    ]
    stats = [
        {"kind": "corpus", "n": 10, "v": 10.0, "term": None},
        {"kind": "term", "n": 2, "v": None, "term": "copper"},
    ]
    idx, fake = make(keyword_handler(recall, stats))
    hits = idx.keyword_search("s", ADVERSARIAL + " copper harb", top_k=2)
    assert [h.doc_id for h in hits] == ["short", "long"]
    assert hits[0].score > hits[1].score > 0

    recall_sql, recall_params = fake.find("ts_rank", "execute")[0][1:]
    assert "to_tsquery('english', :q)" in recall_sql
    assert "ORDER BY rank DESC" in recall_sql
    assert recall_params["recall"] == 200
    assert all(re.fullmatch(r"'[a-z0-9_]+':\*", t) for t in recall_params["q"].split(" | "))
    stats_params = fake.find("search_corpus_stats", "execute")[0][2]
    assert stats_params["terms"] == ["copper"]
    assert_only_placeholders(fake, [ADVERSARIAL, "DROP TABLE search_doc;"])


def test_keyword_search_phrases_report_matches() -> None:
    recall = [
        {
            "doc_id": "d1",
            "doc_kind": "doc",
            "doc_len": 5,
            "tsv_text": "'copper':1",
            "metadata": "{}",
            "body": "b",
            "ph0": True,
            "ph1": False,
        },
    ]
    idx, fake = make(keyword_handler(recall, []))
    hits = idx.keyword_search("s", "", top_k=5, phrases=["master  netting", "force majeure"])
    assert hits[0].matched_phrases == ["master netting"]
    recall_sql, params = fake.find("ts_rank", "execute")[0][1:]
    assert recall_sql.count("phraseto_tsquery('english', :ph") >= 2
    assert params["ph0"] == "master netting"
    assert params["ph1"] == "force majeure"


def test_keyword_search_phrase_only_recall_ranks_by_phrase_rank() -> None:
    idx, fake = make(keyword_handler([], []))
    idx.keyword_search("s", "", top_k=5, phrases=["master netting", "force majeure"])
    sql, params = fake.find("ts_rank", "execute")[0][1:]
    assert "to_tsquery('english', :q)" not in sql
    assert "q" not in params
    assert "GREATEST(ts_rank(tsv, phraseto_tsquery('english', :ph0)), ts_rank(tsv, phraseto_tsquery" in sql
    assert "ORDER BY rank DESC, doc_id LIMIT :recall" in sql
    assert_only_placeholders(fake, [])


def test_keyword_search_mixed_recall_takes_greatest_rank() -> None:
    idx, fake = make(keyword_handler([], []))
    idx.keyword_search("s", "copper", top_k=5, phrases=["master netting"])
    sql = fake.find("ts_rank", "execute")[0][1]
    assert "GREATEST(ts_rank(tsv, to_tsquery('english', :q)), ts_rank(tsv, phraseto_tsquery('english', :ph0)))" in sql


def test_keyword_search_single_phrase_has_no_greatest() -> None:
    idx, fake = make(keyword_handler([], []))
    idx.keyword_search("s", "", top_k=5, phrases=["master netting"])
    sql = fake.find("ts_rank", "execute")[0][1]
    assert "GREATEST" not in sql
    assert "ts_rank(tsv, phraseto_tsquery('english', :ph0)) AS rank" in sql


def test_fuzzy_entities_applies_document_filters_server_side() -> None:
    idx, fake = make()
    idx.fuzzy_entities("s", "Glencore", filters={"ontology_ids": ["o1"], "require_mapped": True})
    sql, params = fake.find("FROM entity_label", "execute")[0][1:]
    assert "EXISTS (SELECT 1 FROM search_doc d WHERE d.namespace = :ns" in sql
    assert "= entity_label.entity_id" in sql
    assert "metadata @> CAST(" in sql
    assert {"ontology_id": "o1"} in params.values()
    assert_only_placeholders(fake, [])


def test_fuzzy_entities_without_filters_has_no_exists() -> None:
    idx, fake = make()
    idx.fuzzy_entities("s", "Glencore")
    assert "EXISTS" not in fake.find("FROM entity_label", "execute")[0][1]


def test_search_passes_filters_to_fuzzy_lookup() -> None:
    idx, fake = make()
    idx.search("s", "copper", vector=[1, 0, 0], filters={"ontology_ids": ["o1"]})
    sql, params = fake.find("FROM entity_label", "execute")[0][1:]
    assert "EXISTS" in sql
    assert {"ontology_id": "o1"} in params.values()


def test_keyword_search_without_tokens_makes_no_call() -> None:
    idx, fake = make()
    assert idx.keyword_search("s", "!!! &&", top_k=5) == []
    assert fake.events == []


def test_keyword_search_no_recall_rows() -> None:
    idx, fake = make(keyword_handler([], []))
    assert idx.keyword_search("s", "copper", top_k=5) == []
    assert not fake.find("search_corpus_stats")


def test_fuzzy_entities_uses_trigram_similarity() -> None:
    rows = [{"entity_id": "e1", "label": "Glencore", "s": 0.7}]

    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return rows if "FROM entity_label" in sql else []

    idx, fake = make(handler)
    hits = idx.fuzzy_entities("s", ADVERSARIAL, limit=5, threshold=0.4)
    assert [(h.entity_id, h.label, h.score) for h in hits] == [("e1", "Glencore", 0.7)]
    set_cfg = fake.find("set_config", "execute")[0]
    assert "pg_trgm.similarity_threshold" in set_cfg[1]
    assert set_cfg[2] == {"th": "0.4"}
    sql = fake.find("FROM entity_label", "execute")[0][1]
    assert "label % :t" in sql
    assert "similarity(label, :t)" in sql
    assert "levenshtein" not in sql
    assert [e[1] for e in fake.events if e[0] == "tx"] == ["BEGIN", "COMMIT"]
    assert_only_placeholders(fake, [ADVERSARIAL])


def test_fuzzy_entities_recheck_adds_levenshtein() -> None:
    idx, fake = make()
    idx.fuzzy_entities("s", "Glencor", recheck=True)
    assert "levenshtein(lower(label), lower(:t)) <= 2" in fake.find("FROM entity_label", "execute")[0][1]


def test_fuzzy_entities_blank_text() -> None:
    idx, fake = make()
    assert idx.fuzzy_entities("s", "  ") == []
    assert fake.events == []


def test_search_maps_filters_to_metadata_containment() -> None:
    idx, fake = make()
    idx.search(
        "s",
        "copper",
        vector=[1, 0, 0],
        mode=SearchMode.HYBRID,
        filters={"ontology_ids": ["o1"], "require_mapped": True},
    )
    selects = [e for e in fake.events if e[0] == "execute" and "FROM search_doc" in e[1]]
    assert selects
    for _, sql, params in selects:
        assert "metadata @> CAST(" in sql
        assert "jsonb_exists(metadata, 'data_source_id')" in sql
        assert {"ontology_id": "o1"} in params.values()


def test_search_hybrid_resolves_trigram_only_entity_to_doc() -> None:
    def handler(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FROM entity_label" in sql:
            return [{"entity_id": "urn:harbor", "label": "Harbor Ltd", "s": 0.6}]
        if "metadata @> ANY" in sql:
            return [
                {"doc_id": "d9", "doc_kind": "entity", "body": "Harbor Ltd", "metadata": {"entity_uri": "urn:harbor"}}
            ]
        return []

    idx, fake = make(handler)
    hits = idx.search("s", "Harbour", vector=[1, 0, 0])
    assert [h.key for h in hits] == ["urn:harbor"]
    assert hits[0].ranks == {"trigram": 1}
    lookup = fake.find("metadata @> ANY", "execute")[0]
    assert lookup[2]["ids"] == ["urn:harbor"]
