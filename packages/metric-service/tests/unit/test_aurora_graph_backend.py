# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""GRAPH_BACKEND=aurora: metric CRUD and class resolution run over the Aurora quad table.

The real SPARQL that ``MetricNeptuneClient`` generates (INSERT DATA, WITH/DELETE/INSERT, DELETE WHERE,
SELECT/ASK) is evaluated by ``coa_common.aurora_sparql`` over an in-memory ``coa_triple``. Any HTTP call
fails the test.
"""

from __future__ import annotations

import httpx
import pytest
from coa_common import aurora_sparql
from coa_common.testing_aurora_graph import InMemoryTripleClient, make_sparql
from coa_metrics import neptune_client
from coa_metrics.neptune_client import (
    OWL,
    RDF,
    RDFS,
    MetricAiContext,
    MetricDefinition,
    MetricDialect,
    MetricNeptuneClient,
    _named_graph,
)

pytestmark = pytest.mark.unit

NS = "demo"


@pytest.fixture
def db(monkeypatch) -> InMemoryTripleClient:
    sparql, client = make_sparql(ttl_s=60)
    monkeypatch.setenv("GRAPH_BACKEND", "aurora")
    monkeypatch.setattr(aurora_sparql, "_shared", sparql)
    monkeypatch.setattr(neptune_client, "NEPTUNE_ENDPOINT", "")

    def refuse(*_a, **_k):
        raise AssertionError("a Neptune HTTP call was made")

    monkeypatch.setattr(httpx.Client, "post", refuse)
    return client


def _metric(name: str = "revenue", **kw) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        description=f"Total {name}",
        expression_dialects=[MetricDialect(dialect="trino", expression="SUM(amount)")],
        data_source_id="ds-1",
        source_table="orders",
        unit="currency",
        ai_context=MetricAiContext(synonyms=["sales"], instructions="by month", examples=["q"]),
        defined_by="steward@example.com",
        effective_from="2026-05-01",
        label="Revenue",
        **kw,
    )


def test_create_get_list_update_delete_round_trip(db):
    client = MetricNeptuneClient()
    client.create_metric(NS, _metric("revenue"))
    client.create_metric(NS, _metric("orders_count"))

    got = client.get_metric(NS, "revenue")
    assert got is not None
    assert got.description == "Total revenue"
    assert got.expression_dialects == [MetricDialect(dialect="trino", expression="SUM(amount)")]
    assert got.ai_context and got.ai_context.synonyms == ["sales"]
    assert got.effective_from == "2026-05-01"
    assert got.label == "Revenue"

    listed = client.list_metrics(NS)
    names = [m.name for m in (listed[0] if isinstance(listed, tuple) else listed)]
    assert sorted(names) == ["orders_count", "revenue"]
    assert sorted(m.name for m in client.get_metrics_bulk(NS)) == ["orders_count", "revenue"]
    assert [m.name for m in client.get_metrics_bulk(NS, ["revenue"])] == ["revenue"]

    changed = _metric("revenue")
    changed.description = "Net revenue"
    client.update_metric(NS, "revenue", changed)
    assert client.get_metric(NS, "revenue").description == "Net revenue"

    assert client.delete_metric(NS, "revenue") is True
    assert client.get_metric(NS, "revenue") is None
    assert client.delete_metric(NS, "revenue") is False
    # the other metric is untouched, and everything is stored in the namespace's metric graph
    assert client.get_metric(NS, "orders_count") is not None
    assert {r["graph_uri"] for r in db.rows} == {_named_graph(NS)}
    assert {r["namespace"] for r in db.rows} == {NS}


def test_delete_all_metrics_for_namespace_counts_subjects(db):
    client = MetricNeptuneClient()
    client.create_metric(NS, _metric("a"))
    client.create_metric(NS, _metric("b"))
    client.create_metric("other", _metric("c"))
    assert client.delete_all_metrics_for_namespace(NS) == 2
    assert client.delete_all_metrics_for_namespace(NS) == 0
    assert client.get_metric("other", "c") is not None


def test_create_seeds_the_base_vocabulary_once(db):
    client = MetricNeptuneClient()
    client.create_metric(NS, _metric("a"))
    client.create_metric(NS, _metric("b"))
    base = [
        r for r in db.rows if r["p"] == RDF + "type" and r["o"] == OWL + "Class" and r["s"].endswith("GovernedMetric")
    ]
    assert len(base) == 1


def test_resolve_class_uris_reads_every_graph_of_the_namespace(db):
    db.load_turtle(
        """
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <http://ex.org/Order> a owl:Class ; rdfs:label "Order", "Bestellung"@de .
        """,
        f"{aurora_sparql.graph_base()}/{NS}/ontology-a",
        NS,
    )
    db.add_triple(
        f"{aurora_sparql.graph_base()}/other/ontology-b",
        "http://ex.org/Secret",
        RDFS + "label",
        "Order",
        o_kind="literal",
        namespace="other",
    )
    db.add_triple(
        f"{aurora_sparql.graph_base()}/other/ontology-b",
        "http://ex.org/Secret",
        RDF + "type",
        OWL + "Class",
        namespace="other",
    )
    client = MetricNeptuneClient()
    assert client.resolve_class_uris(NS, ["Order", "Bestellung", "Unknown"]) == [
        "http://ex.org/Order",
        "http://ex.org/Order",
    ]


def test_default_flag_still_requires_the_neptune_endpoint(monkeypatch):
    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    monkeypatch.setattr(neptune_client, "NEPTUNE_ENDPOINT", "")
    with pytest.raises(RuntimeError, match="NEPTUNE_ENDPOINT not set"):
        neptune_client._sparql_query("ASK { ?s ?p ?o }")
    with pytest.raises(RuntimeError, match="NEPTUNE_ENDPOINT not set"):
        neptune_client._sparql_update("INSERT DATA { <urn:a> <urn:b> <urn:c> }")
