# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AuroraVectorStore: SQL shape, parameters, padding, limits (no database)."""

from __future__ import annotations

import json

import pytest
from coa_common.aurora_data_api import AuroraConfigError, DataApiClient
from coa_ontology.stores.aurora.schema import SchemaInfo, ddl_statements, ensure_schema, reset_schema_cache
from coa_ontology.stores.aurora.vector import AuroraVectorStore, vector_text
from coa_ontology.stores.base import VectorStore

from .aurora_fakes import CONFIG, ScriptedRds, once_then, too_large

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    reset_schema_cache()


def _store(rds: ScriptedRds, namespace: str | None = "ns1") -> AuroraVectorStore:
    return AuroraVectorStore(namespace=namespace, client=rds.client(), dimensions=8)


def _item(uri="http://ex.org/A", **kw):
    base = {
        "entity_uri": uri,
        "ontology_id": "onto1",
        "entity_type": "class",
        "embedding_type": "text",
        "model_id": "m1",
        "vector": [1.0, 0.0, 0.0],
        "text": "A thing",
    }
    return {**base, **kw}


def test_is_a_vector_store():
    assert isinstance(_store(ScriptedRds()), VectorStore)
    assert _store(ScriptedRds()).dimensions == 8


def test_schema_checked_once_with_one_round_trip_when_current():
    rds = ScriptedRds()
    s = _store(rds)
    s.delete_index()
    s.delete_index()
    assert len(rds.sql_calls("coa_schema_meta")) == 1
    assert not rds.sql_calls("CREATE")


def test_schema_applied_in_one_locked_transaction_when_missing():
    rds = ScriptedRds(schema_row=None)
    rds.when("FROM coa_schema_meta", [{"version": None}])
    rds.when("SELECT\n  (SELECT extversion", [{"pgvector": "0.7.4", "dimensions": 8}])
    info = ensure_schema(rds.client(), 8)
    assert info == SchemaInfo("1", 8, "0.7.4")
    assert not info.supports_iterative_scan
    assert rds.events == ["begin", "commit"]
    sql = [c["sql"] for c in rds.calls]
    assert any("pg_advisory_xact_lock" in s for s in sql)
    assert any("vector(8)" in s for s in sql)
    assert any("coa_embedding_hnsw" in s and "vector_cosine_ops" in s for s in sql)


def test_dimension_mismatch_raises():
    rds = ScriptedRds()
    with pytest.raises(RuntimeError, match=r"vector\(8\).*is 16"):
        ensure_schema(rds.client(), 16)


def test_ddl_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        ddl_statements(0)
    with pytest.raises(ValueError):
        ddl_statements(5000)


def test_upsert_is_one_parameterised_statement_with_padded_vector():
    rds = ScriptedRds()
    out = _store(rds).store_embedding(_item())
    (call,) = rds.sql_calls("ON CONFLICT")
    assert "http://ex.org/A" not in call["sql"]
    assert "ON CONFLICT (namespace, ontology_id, entity_uri, embedding_type, model_id) DO UPDATE" in call["sql"]
    (row,) = json.loads(call["params"]["rows"])
    assert row["namespace"] == "ns1"
    assert row["embedding"] == vector_text([1.0, 0, 0, 0, 0, 0, 0, 0])
    assert out["embedding"] == [1.0, 0, 0, 0, 0, 0, 0, 0]
    assert out["namespace"] == "ns1"


def test_batch_dedupes_keys_and_keeps_the_last():
    rds = ScriptedRds()
    _store(rds).store_embeddings_batch([_item(text="old"), _item(text="new"), _item("http://ex.org/B")])
    (call,) = rds.sql_calls("ON CONFLICT")
    rows = json.loads(call["params"]["rows"])
    assert {r["entity_uri"]: r["text"] for r in rows} == {"http://ex.org/A": "new", "http://ex.org/B": "A thing"}


def test_batch_spanning_chunks_uses_one_transaction():
    rds = ScriptedRds()
    items = [_item(f"http://ex.org/{i}", text="x" * 400_000) for i in range(8)]
    _store(rds).store_embeddings_batch(items)
    assert len(rds.sql_calls("ON CONFLICT")) >= 2
    assert rds.events == ["begin", "commit"]


def test_empty_batch_does_nothing():
    rds = ScriptedRds()
    assert _store(rds).store_embeddings_batch([]) == []
    assert rds.calls == []


def test_vector_longer_than_dimensions_is_rejected():
    with pytest.raises(ValueError, match="at most 8"):
        _store(ScriptedRds()).store_embedding(_item(vector=[0.0] * 9))


def test_per_call_namespace_wins_over_bound_and_default_applies():
    rds = ScriptedRds()
    s = _store(rds, namespace="bound")
    s.delete_embeddings_for_ontology("o", namespace="explicit")
    s.delete_embeddings_for_ontology("o")
    assert [c["params"]["ns"] for c in rds.sql_calls("DELETE FROM coa_embedding")] == ["explicit", "bound"]
    unbound = _store(rds, namespace=None)
    unbound.delete_embeddings_for_ontology("o")
    assert rds.sql_calls("DELETE FROM coa_embedding")[-1]["params"]["ns"] == "default"


def test_search_returns_raw_cosine_and_parses_vectors():
    rds = ScriptedRds()
    rds.when(
        "<=>",
        [
            {
                "id": 2,
                "entity_uri": "u1",
                "embedding_type": "text",
                "model_id": "m1",
                "ontology_id": "o",
                "entity_type": "class",
                "text": "t",
                "embedding": "[1,0,0,0,0,0,0,0]",
                "score": 0.97,
            },
            {
                "id": 3,
                "entity_uri": "u2",
                "embedding_type": "text",
                "model_id": "m1",
                "ontology_id": "",
                "entity_type": None,
                "text": "",
                "embedding": "[0,1,0,0,0,0,0,0]",
                "score": -0.25,
            },
        ],
    )
    hits = _store(rds).search_nearest([1.0, 0.0], "text", "m1", top_k=2)
    assert [h.score for h in hits] == [0.97, -0.25]
    assert hits[0].vector[0] == 1
    assert hits[1].ontology_id is None
    call = rds.sql_calls("<=>")[0]
    assert "1 - (embedding <=> CAST(CAST(:v AS text) AS vector))" in call["sql"]
    assert call["params"]["v"].startswith("[1.0,0.0,0.0")
    assert call["params"]["k"] == 2
    assert call["tx"] is None


def test_filtered_search_uses_iterative_scan_inside_a_transaction_when_supported():
    rds = ScriptedRds()
    _store(rds).search_nearest([1.0], "text", "m1", entity_type="class", ontology_id="o", top_k=5)
    sets = [c["sql"] for c in rds.calls if "set_config" in c["sql"]]
    assert any("hnsw.iterative_scan" in s for s in sets)
    assert rds.events == ["begin", "commit"]
    q = rds.sql_calls("<=>")[0]
    assert "entity_type = :et" in q["sql"] and "ontology_id = :oid" in q["sql"]
    assert q["tx"] == "tx-1"


def test_filtered_search_skips_iterative_scan_before_pgvector_0_8():
    rds = ScriptedRds(schema_row={"version": "1", "pgvector": "0.7.4", "dimensions": 8})
    _store(rds).search_nearest([1.0], "text", "m1", entity_type="class", top_k=5)
    assert not any("hnsw.iterative_scan" in c["sql"] for c in rds.calls)
    assert rds.events == []


def test_large_top_k_raises_ef_search():
    rds = ScriptedRds()
    _store(rds).search_nearest([1.0], "text", "m1", top_k=200)
    ef = [c for c in rds.calls if "hnsw.ef_search" in c["sql"]]
    assert ef and ef[0]["params"]["v"] == "200"


def test_search_falls_back_to_ranking_then_vectors_when_response_too_large():
    rds = ScriptedRds()
    ranked = [
        {
            "id": 7,
            "entity_uri": "u",
            "embedding_type": "text",
            "model_id": "m1",
            "ontology_id": "o",
            "entity_type": "class",
            "text": "t",
            "score": 0.5,
        }
    ]
    rds.when("<=>", once_then(too_large(), ranked))
    rds.when("jsonb_array_elements_text", [{"id": 7, "embedding": "[0.5,0.5]"}])
    (hit,) = _store(rds).search_nearest([1.0], "text", "m1", top_k=3)
    assert hit.vector == [0.5, 0.5]
    assert json.loads(rds.sql_calls("jsonb_array_elements_text")[0]["params"]["ids"]) == [7]


def test_search_top_k_below_one_is_empty_without_queries():
    rds = ScriptedRds()
    assert _store(rds).search_nearest([1.0], "t", "m", top_k=0) == []
    assert rds.calls == []


def test_list_with_limit_is_metadata_only_and_bounded():
    rds = ScriptedRds()
    rds.when("FROM coa_embedding", [{"id": 1, "entity_uri": "u", "ontology_id": "o", "text": "t"}])
    out = _store(rds).list_embeddings_for_ontology("o", limit=5)
    assert out == [{"entity_uri": "u", "ontology_id": "o", "text": "t"}]
    call = rds.sql_calls("FROM coa_embedding")[-1]
    assert "embedding::text" not in call["sql"]
    assert call["params"]["limit"] == 5
    assert _store(rds).list_embeddings_for_ontology("o", limit=0) == []


def test_list_without_limit_returns_documents_with_vectors():
    rds = ScriptedRds()
    rds.when(
        "FROM coa_embedding",
        [
            {
                "id": 1,
                "entity_uri": "u",
                "ontology_id": "o",
                "entity_type": "class",
                "embedding_type": "text",
                "model_id": "m",
                "namespace": "ns1",
                "text": "t",
                "context_text": None,
                "data_source_id": "ds",
                "query_backend": None,
                "embedding": "[1,2]",
            }
        ],
    )
    (doc,) = _store(rds).list_embeddings_for_ontology("o", entity_type="class", embedding_type="text")
    assert doc["embedding"] == [1, 2]
    assert doc["data_source_id"] == "ds"
    assert "context_text" not in doc


def test_get_embeddings_for_entity_filters_are_parameters():
    rds = ScriptedRds()
    _store(rds).get_embeddings_for_entity("http://ex.org/A", entity_type="class", embedding_type="text")
    call = rds.sql_calls("FROM coa_embedding")[-1]
    assert "http://ex.org/A" not in call["sql"]
    assert call["params"]["uri"] == "http://ex.org/A"
    assert call["params"]["et"] == "class" and call["params"]["emb"] == "text"


def test_list_searchable_entity_uris_pages_by_keyset():
    rds = ScriptedRds()
    pages = [[{"entity_uri": f"u{i:04d}"} for i in range(2000)], [{"entity_uri": "z"}]]
    rds.when("SELECT DISTINCT entity_uri", lambda sql, p: pages.pop(0))
    uris = _store(rds).list_searchable_entity_uris("o")
    assert len(uris) == 2001
    calls = rds.sql_calls("SELECT DISTINCT entity_uri")
    assert calls[1]["params"]["after"] == "u1999"


def test_delete_runs_in_bounded_batches_until_a_short_batch():
    rds = ScriptedRds()
    counts = [5000, 5000, 17]
    rds.when("DELETE FROM coa_embedding", lambda sql, p: [{"n": counts.pop(0)}])
    assert _store(rds).delete_embeddings_for_ontology("o") == 10017
    assert all(c["params"]["batch"] == 5000 for c in rds.sql_calls("DELETE FROM coa_embedding"))


def test_delete_for_entities_is_scoped_to_the_ontology_and_skips_empty_input():
    rds = ScriptedRds()
    rds.when("DELETE FROM coa_embedding", [{"n": 2}])
    s = _store(rds)
    assert s.delete_embeddings_for_entities([], "o") == 0
    assert s.delete_embeddings_for_entities(["a"], "") == 0
    assert not rds.sql_calls("DELETE")
    assert s.delete_embeddings_for_entities(["a", "a", "b"], "o") == 2
    call = rds.sql_calls("DELETE FROM coa_embedding")[0]
    assert "ontology_id = :oid" in call["sql"]
    assert json.loads(call["params"]["uris"]) == ["a", "b"]


def test_delete_index_reports_whether_rows_existed():
    rds = ScriptedRds()
    s = _store(rds)
    rds.when("DELETE FROM coa_embedding", [{"n": 0}])
    assert s.delete_index() is False
    rds.rules.insert(0, ("DELETE FROM coa_embedding", [{"n": 3}]))
    assert s.delete_index() is True


def test_health_check_variants():
    rds = ScriptedRds()
    assert _store(rds).health_check() == {"status": "ok"}

    class Boom:
        def execute(self, *a, **k):
            raise AuroraConfigError("no config")

    s = AuroraVectorStore(client=Boom(), dimensions=8)  # type: ignore[arg-type]
    assert s.health_check() == {"status": "unconfigured"}

    class Err:
        def execute(self, *a, **k):
            raise RuntimeError("down")

    s2 = AuroraVectorStore(client=Err(), dimensions=8)  # type: ignore[arg-type]
    assert s2.health_check()["status"] == "error"


def test_constructor_does_not_resolve_config_or_touch_aws(monkeypatch):
    for k in ("AURORA_CLUSTER_ARN", "AURORA_SECRET_ARN", "AURORA_CLUSTER_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    store = AuroraVectorStore(namespace="x")
    assert store.dimensions >= 1
    assert isinstance(DataApiClient(CONFIG), DataApiClient)
