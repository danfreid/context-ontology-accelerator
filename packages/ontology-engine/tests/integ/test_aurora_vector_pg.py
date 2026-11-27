# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AuroraVectorStore against a real pgvector container (through the Data API fake)."""

from __future__ import annotations

import math

import pytest

from .parity_support import aurora_vector_store

pytestmark = pytest.mark.integ

DIMS = 8


def unit(*xs: float) -> list[float]:
    """Unit-length vector padded to DIMS."""
    v = list(xs) + [0.0] * (DIMS - len(xs))
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def item(uri: str, vec: list[float], *, onto: str = "o1", etype: str = "class", text: str = "", **kw):
    return {
        "entity_uri": uri,
        "ontology_id": onto,
        "entity_type": etype,
        "embedding_type": "text",
        "model_id": "m1",
        "vector": vec,
        "text": text or uri,
        **kw,
    }


@pytest.fixture
def store(aurora_client):
    return aurora_vector_store(aurora_client, "ns1", DIMS)


def test_upsert_is_idempotent_and_replaces(store):
    store.store_embedding(item("http://ex/A", unit(1), text="first"))
    store.store_embedding(item("http://ex/A", unit(0, 1), text="second"))
    docs = store.get_embeddings_for_entity("http://ex/A")
    assert len(docs) == 1
    assert docs[0]["text"] == "second"
    assert docs[0]["embedding"][1] == pytest.approx(1.0)
    assert docs[0]["namespace"] == "ns1"


def test_nearest_ranks_by_cosine_with_raw_scores(store):
    store.store_embeddings_batch(
        [
            item("http://ex/same", unit(1, 0)),
            item("http://ex/near", unit(1, 1)),
            item("http://ex/orth", unit(0, 1)),
            item("http://ex/opp", unit(-1, 0)),
        ]
    )
    hits = store.search_nearest(unit(1, 0), "text", "m1", top_k=4)
    assert [h.entity_uri for h in hits] == ["http://ex/same", "http://ex/near", "http://ex/orth", "http://ex/opp"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(math.sqrt(0.5))
    assert hits[2].score == pytest.approx(0.0, abs=1e-9)
    assert hits[3].score == pytest.approx(-1.0)
    assert hits[0].vector == []  # like the AOSS store, hits carry no vector
    assert hits[0].ontology_id == "o1" and hits[0].entity_type == "class"
    assert len(store.search_nearest(unit(1, 0), "text", "m1", top_k=2)) == 2


def test_short_query_vector_is_rejected(store):
    """Parity difference: A padded short query vectors; the search layer requires the configured width."""
    store.store_embedding(item("http://ex/A", unit(1)))
    with pytest.raises(ValueError, match="dimensions"):
        store.search_nearest([1.0], "text", "m1", top_k=1)


def test_filters_by_entity_type_ontology_model_and_namespace(store):
    store.store_embeddings_batch(
        [
            item("http://ex/c1", unit(1, 0), etype="class", onto="o1"),
            item("http://ex/p1", unit(1, 0.1), etype="property", onto="o1"),
            item("http://ex/c2", unit(1, 0.2), etype="class", onto="o2"),
            item("http://ex/other-model", unit(1, 0.3), model_id="m2"),
        ]
    )
    other_ns = aurora_vector_store(store._client, "ns2", DIMS)
    other_ns.store_embedding(item("http://ex/leak", unit(1, 0)))

    uris = lambda hits: {h.entity_uri for h in hits}  # noqa: E731
    assert uris(store.search_nearest(unit(1), "text", "m1", top_k=10)) == {
        "http://ex/c1",
        "http://ex/p1",
        "http://ex/c2",
    }
    assert uris(store.search_nearest(unit(1), "text", "m1", entity_type="property", top_k=10)) == {"http://ex/p1"}
    assert uris(store.search_nearest(unit(1), "text", "m1", ontology_id="o2", top_k=10)) == {"http://ex/c2"}
    assert uris(store.search_nearest(unit(1), "text", "m2", top_k=10)) == {"http://ex/other-model"}
    assert uris(store.search_nearest(unit(1), "text", "m1", namespace="ns2", top_k=10)) == {"http://ex/leak"}
    assert store.search_nearest(unit(1), "other-type", "m1") == []


def test_get_list_and_limit(store):
    store.store_embeddings_batch(
        [item(f"http://ex/e{i}", unit(1, i + 1), etype="class" if i % 2 else "property") for i in range(6)]
    )
    assert len(store.get_embeddings_for_entity("http://ex/e1", entity_type="class")) == 1
    assert store.get_embeddings_for_entity("http://ex/e1", entity_type="property") == []
    assert store.get_embeddings_for_entity("http://ex/e1", embedding_type="nope") == []
    assert len(store.list_embeddings_for_ontology("o1")) == 6
    assert len(store.list_embeddings_for_ontology("o1", entity_type="class")) == 3
    assert len(store.list_embeddings_for_ontology("o1", embedding_type="text")) == 6
    limited = store.list_embeddings_for_ontology("o1", limit=2)
    assert len(limited) == 2
    assert {"entity_uri", "ontology_id", "text"} <= set(limited[0])
    assert "embedding" not in limited[0]
    assert store.list_embeddings_for_ontology("o1", limit=0) == []
    assert store.list_embeddings_for_ontology("missing") == []


def test_list_searchable_entity_uris_is_distinct_and_scoped(store):
    store.store_embeddings_batch(
        [
            item("http://ex/b", unit(1)),
            item("http://ex/a", unit(1, 1)),
            item("http://ex/a", unit(1, 2), model_id="m2"),
            item("http://ex/x", unit(1), onto="o2"),
        ]
    )
    assert store.list_searchable_entity_uris("o1") == ["http://ex/a", "http://ex/b"]
    assert store.list_searchable_entity_uris("nope") == []


def test_delete_for_entities_is_scoped_to_one_ontology(store):
    store.store_embeddings_batch(
        [
            item("http://ex/shared", unit(1), onto="o1"),
            item("http://ex/shared", unit(1, 1), onto="o2"),
            item("http://ex/keep", unit(1), onto="o1"),
        ]
    )
    assert store.delete_embeddings_for_entities(["http://ex/shared"], "o1") == 1
    assert [d["ontology_id"] for d in store.get_embeddings_for_entity("http://ex/shared")] == ["o2"]
    assert len(store.get_embeddings_for_entity("http://ex/keep")) == 1


def test_delete_for_ontology_and_delete_index(store):
    store.store_embeddings_batch(
        [item(f"http://ex/e{i}", unit(1, i)) for i in range(5)] + [item("http://ex/z", unit(1), onto="o2")]
    )
    assert store.delete_embeddings_for_ontology("o1") == 5
    assert store.list_embeddings_for_ontology("o1") == []
    assert len(store.list_embeddings_for_ontology("o2")) == 1
    other = aurora_vector_store(store._client, "ns2", DIMS)
    other.store_embedding(item("http://ex/n2", unit(1)))
    assert store.delete_index() is True
    assert store.list_embeddings_for_ontology("o2") == []
    assert len(other.list_embeddings_for_ontology("o1")) == 1
    assert store.delete_index() is False


def test_optional_columns_round_trip(store):
    store.store_embedding(
        item("http://ex/G", unit(1), context_text="ctx", data_source_id="src-1", query_backend="RDF_SPARQL")
    )
    (doc,) = store.get_embeddings_for_entity("http://ex/G")
    assert (doc["context_text"], doc["data_source_id"], doc["query_backend"]) == ("ctx", "src-1", "RDF_SPARQL")


def test_pagination_under_a_small_response_cap(store, rds_fake):
    n = 40
    store.store_embeddings_batch([item(f"http://ex/e{i:03d}", unit(1, i + 1), text="t" * 50) for i in range(n)])
    rds_fake.max_response_bytes = 2500  # a handful of documents per response
    docs = store.list_embeddings_for_ontology("o1")
    assert sorted(d["entity_uri"] for d in docs) == [f"http://ex/e{i:03d}" for i in range(n)]
    assert len(store.list_searchable_entity_uris("o1")) == n
    rds_fake.max_response_bytes = (
        10_000  # 40 documents do not fit, the 40 ranked ids do: rank first, then fetch 25 at a time
    )
    hits = store.search_nearest(unit(1, 5), "text", "m1", top_k=n)
    assert len(hits) == n
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_health_check(store):
    assert store.health_check() == {"status": "ok"}


def test_dimension_mismatch_with_stored_vectors_is_rejected(store, aurora_client):
    from coa_ontology.stores.aurora.schema import AuroraSchemaError, check_schema, reset_schema_cache

    store.store_embedding(item("http://ex/A", unit(1)))
    reset_schema_cache()
    try:
        with pytest.raises(AuroraSchemaError, match="8-dimension"):
            check_schema(aurora_client, 16)
        assert check_schema(aurora_client, DIMS).stored_dimensions == DIMS
    finally:
        reset_schema_cache()
