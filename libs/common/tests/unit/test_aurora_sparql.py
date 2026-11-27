# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""SPARQL over the Aurora quad table: scope extraction, result shapes, cache and update diff."""

from __future__ import annotations

import pytest
from coa_common.aurora_sparql import (
    AuroraSparql,
    AuroraSparqlError,
    aurora_graph_enabled,
    scope_of,
)
from coa_common.testing_aurora_graph import InMemoryTripleClient, make_sparql

BASE = "https://ontology-workbench.local"
G1 = f"{BASE}/demo/onto-1"
G2 = f"{BASE}/demo/onto-2"
OTHER = f"{BASE}/other/onto-1"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"

TTL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://ex.org/Order> a owl:Class ; rdfs:label "Order", "Bestellung"@de .
<http://ex.org/Customer> a owl:Class ; rdfs:label "Customer" .
"""


@pytest.fixture
def store() -> tuple[AuroraSparql, InMemoryTripleClient]:
    sparql, client = make_sparql(ttl_s=60)
    client.load_turtle(TTL, G1, "demo")
    client.add_triple(G2, "http://ex.org/Policy", RDF_TYPE, OWL_CLASS, namespace="demo")
    client.add_triple(OTHER, "http://ex.org/Secret", RDF_TYPE, OWL_CLASS, namespace="other")
    return sparql, client


def _loads(client: InMemoryTripleClient) -> int:
    return sum(1 for sql, _ in client.statements if sql.startswith("SELECT id,"))


def test_scope_of_reads_graph_iris_and_strstarts_prefixes():
    exact, prefixes = scope_of(
        f"SELECT * WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} GRAPH ?g {{ ?a ?b ?c }} "
        f'FILTER(STRSTARTS(STR(?g), "{BASE}/demo/")) }}'
    )
    assert exact == ()  # covered by the prefix
    assert prefixes == (f"{BASE}/demo/",)
    assert scope_of(f"WITH <{G1}> DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o }}") == ((G1,), ())
    assert scope_of("SELECT * WHERE { ?s ?p ?o }") == ((), ())


def test_query_returns_flat_dicts_and_loads_only_the_named_graph(store):
    sparql, client = store
    rows = sparql.query(f"SELECT ?c ?l WHERE {{ GRAPH <{G1}> {{ ?c <{RDFS_LABEL}> ?l }} }} ORDER BY ?l")
    assert sorted(rows, key=lambda r: r["l"]) == [
        {"c": "http://ex.org/Order", "l": "Bestellung"},
        {"c": "http://ex.org/Customer", "l": "Customer"},
        {"c": "http://ex.org/Order", "l": "Order"},
    ]
    assert "graphs" in client.statements[0][1]
    assert "Secret" not in str(rows)


def test_query_appends_limit_only_when_missing(store):
    sparql, _ = store
    q = f"SELECT ?c WHERE {{ GRAPH <{G1}> {{ ?c a <{OWL_CLASS}> }} }}"
    assert len(sparql.query(q, max_results=1)) == 1
    assert len(sparql.query(q + " LIMIT 2", max_results=1)) == 2


def test_prefix_scope_reads_every_graph_of_the_namespace_but_not_others(store):
    sparql, _ = store
    rows = sparql.query(
        f'SELECT ?s WHERE {{ GRAPH ?g {{ ?s a <{OWL_CLASS}> }} FILTER(STRSTARTS(STR(?g), "{BASE}/demo/")) }} '
        "ORDER BY ?s"
    )
    assert [r["s"] for r in rows] == ["http://ex.org/Customer", "http://ex.org/Order", "http://ex.org/Policy"]


def test_ask_and_select_json(store):
    sparql, _ = store
    assert sparql.ask(f"ASK {{ GRAPH <{G1}> {{ <http://ex.org/Order> a <{OWL_CLASS}> }} }}") is True
    assert sparql.ask(f"ASK {{ GRAPH <{G1}> {{ <http://ex.org/Nope> a <{OWL_CLASS}> }} }}") is False
    data = sparql.query_json(f"SELECT ?l WHERE {{ GRAPH <{G1}> {{ <http://ex.org/Order> <{RDFS_LABEL}> ?l }} }}")
    values = {(b["l"]["type"], b["l"]["value"], b["l"].get("xml:lang")) for b in data["results"]["bindings"]}
    assert values == {("literal", "Order", None), ("literal", "Bestellung", "de")}
    assert data["head"]["vars"] == ["l"]


def test_construct_returns_sorted_triples_capped(store):
    sparql, _ = store
    q = f"CONSTRUCT {{ ?c <{RDFS_LABEL}> ?l }} WHERE {{ GRAPH <{G1}> {{ ?c <{RDFS_LABEL}> ?l }} }}"
    triples = sparql.construct(q)
    assert triples[0] == {"subject": "http://ex.org/Customer", "predicate": RDFS_LABEL, "object": "Customer"}
    assert len(triples) == 3
    assert len(sparql.construct(q, max_triples=2)) == 2


def test_a_bad_query_raises_aurora_sparql_error(store):
    sparql, _ = store
    with pytest.raises(AuroraSparqlError):
        sparql.query("SELEKT nonsense")


def test_cache_serves_repeats_and_expires_and_invalidates(store):
    sparql, client = store
    q = f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}"
    sparql.ask(q)
    sparql.ask(q)
    assert _loads(client) == 1
    sparql.invalidate()
    sparql.ask(q)
    assert _loads(client) == 2
    short = AuroraSparql(client, ttl_s=0)
    short.ask(q)
    short.ask(q)
    assert _loads(client) == 4


def test_cache_is_bounded_by_triple_count(store):
    _, client = store
    sparql = AuroraSparql(client, ttl_s=60, max_cached_triples=4)
    sparql.ask(f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")  # 5 triples: served, not cached
    sparql.ask(f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")
    assert _loads(client) == 2
    sparql.ask(f"ASK {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}")  # 1 triple: cached
    sparql.ask(f"ASK {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}")
    assert _loads(client) == 3


def test_load_larger_than_the_hard_limit_fails(store):
    _, client = store
    sparql = AuroraSparql(client, max_load_triples=2)
    with pytest.raises(AuroraSparqlError, match="narrow"):
        sparql.ask(f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")


def test_insert_data_writes_rows_in_one_transaction_and_invalidates(store):
    sparql, client = store
    q = f'ASK {{ GRAPH <{G1}> {{ <http://ex.org/New> <{RDFS_LABEL}> "fresh" }} }}'
    assert sparql.ask(q) is False
    result = sparql.update(
        f'INSERT DATA {{ GRAPH <{G1}> {{ <http://ex.org/New> <{RDFS_LABEL}> "fresh" . '
        f"<http://ex.org/New> <{RDF_TYPE}> <{OWL_CLASS}> }} }}"
    )
    assert result == {"inserted": 2, "deleted": 0}
    assert client.transactions == 1
    assert sparql.ask(q) is True  # the cache was dropped
    stored = [r for r in client.rows if r["s"] == "http://ex.org/New"]
    assert {r["namespace"] for r in stored} == {"demo"}  # taken from the graph's other rows
    assert {(r["o_kind"], r["o_lang"], r["o_datatype"]) for r in stored} == {("literal", "", ""), ("iri", "", "")}


def test_new_graph_namespace_comes_from_the_graph_base(store):
    sparql, client = store
    new_graph = f"{BASE}/fresh-ns/onto"
    sparql.update(f"INSERT DATA {{ GRAPH <{new_graph}> {{ <http://ex.org/A> <{RDF_TYPE}> <{OWL_CLASS}> }} }}")
    assert [r["namespace"] for r in client.rows if r["graph_uri"] == new_graph] == ["fresh-ns"]


def test_modify_with_delete_insert_diffs_to_the_minimum(store):
    sparql, client = store
    before = client.graph_triples(G1)
    result = sparql.update(
        f"WITH <{G1}> DELETE {{ <http://ex.org/Customer> <{RDFS_LABEL}> ?l }} "
        f'INSERT {{ <http://ex.org/Customer> <{RDFS_LABEL}> "Client" }} '
        f"WHERE {{ <http://ex.org/Customer> <{RDFS_LABEL}> ?l }}"
    )
    assert result == {"inserted": 1, "deleted": 1}
    after = client.graph_triples(G1)
    assert after - before == {("http://ex.org/Customer", RDFS_LABEL, "Client", "literal")}
    assert before - after == {("http://ex.org/Customer", RDFS_LABEL, "Customer", "literal")}


def test_delete_where_on_an_empty_graph_writes_nothing(store):
    sparql, client = store
    rows_before = list(client.rows)
    assert sparql.update(f"DELETE WHERE {{ GRAPH <{BASE}/demo/empty> {{ ?s ?p ?o }} }}") == {
        "inserted": 0,
        "deleted": 0,
    }
    assert client.rows == rows_before


def test_update_keeps_the_lock_and_rolls_back_on_failure(store):
    sparql, client = store
    rows_before = list(client.rows)
    with pytest.raises(AuroraSparqlError):
        sparql.update("INSERT DATA { nonsense")
    assert client.rows == rows_before
    assert any("pg_advisory_xact_lock" in sql for sql, _ in client.statements)


def test_xsd_string_is_stored_as_a_plain_literal(store):
    sparql, client = store
    sparql.update(
        f"INSERT DATA {{ GRAPH <{G2}> {{ <http://ex.org/Policy> <{RDFS_LABEL}> "
        f'"typed"^^<http://www.w3.org/2001/XMLSchema#string> }} }}'
    )
    row = next(r for r in client.rows if r["o"] == "typed")
    assert row["o_datatype"] == ""


def test_turtle_of_one_graph(store):
    sparql, _ = store
    ttl = sparql.turtle(G1)
    assert ttl is not None and "Bestellung" in ttl
    assert sparql.turtle(f"{BASE}/demo/none") is None


def test_health_check_reports_ok_and_error(store):
    sparql, _ = store
    assert sparql.health_check() == {"status": "ok"}

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("down")

    assert AuroraSparql(Broken()).health_check()["status"] == "error"  # type: ignore[arg-type]


def test_graph_backend_flag(monkeypatch):
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    assert aurora_graph_enabled() is False
    monkeypatch.setenv("GRAPH_BACKEND", " Aurora ")
    assert aurora_graph_enabled() is True
    monkeypatch.setenv("GRAPH_BACKEND", "neptune")
    assert aurora_graph_enabled() is False
