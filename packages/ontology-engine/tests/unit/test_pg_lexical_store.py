# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AuroraLexicalStore: SQL backend of the LexicalGraphStore protocol for the Aurora document graph."""

from __future__ import annotations

import pytest
from coa_ontology.inducer.unstructured.stores.pg_lexical import AuroraLexicalStore
from coa_ontology.inducer.unstructured.stores.protocol import LexicalGraphStore

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.rows


def test_satisfies_protocol():
    assert isinstance(AuroraLexicalStore(client=_Client([])), LexicalGraphStore)


def test_rejects_bad_tenant_and_schema():
    with pytest.raises(ValueError):
        AuroraLexicalStore(client=_Client([]), tenant_id="abc'; DROP")
    with pytest.raises(ValueError):
        AuroraLexicalStore(client=_Client([]), schema="lg;x")


def test_class_nodes_bind_tenant():
    c = _Client([{"value": "Company", "count": 3}, {"value": None, "count": 1}])
    out = AuroraLexicalStore(client=c, tenant_id="abc").get_class_nodes()
    assert [(r.value, r.count) for r in out] == [("Company", 3)]
    assert c.calls[0][1]["tenant"] == "abc" and "lg.node" in c.calls[0][0]


def test_entities_by_class_bind_params():
    c = _Client([{"entity_id": "e1", "value": "Acme", "classification": "Company"}])
    out = AuroraLexicalStore(client=c).get_entities_by_class("Company", limit=5)
    assert out[0].entity_id == "e1"
    assert c.calls[0][1]["classification"] == "Company" and c.calls[0][1]["limit"] == 5


def test_facts_spo_and_empty():
    store = AuroraLexicalStore(client=_Client([]))
    assert store.get_facts_for_entities([]) == []
    c = _Client(
        [
            {
                "subject_value": "Acme",
                "subject_class": "Company",
                "fact_value": "Acme acquired Beta",
                "object_value": "Beta",
                "object_class": "Company",
            }
        ]
    )
    facts = AuroraLexicalStore(client=c).get_facts_for_entities(["e1"])
    assert facts and facts[0].object_value == "Beta" and facts[0].predicate
    assert c.calls[0][1]["entity_ids"] == ["e1"]


def test_topics_and_schema_zero_classes():
    c = _Client([{"topic_id": "t1", "value": "Topic", "statement_count": 2}])
    assert AuroraLexicalStore(client=c).get_topics()[0].statement_count == 2
    with pytest.raises(ValueError, match="zero class"):
        AuroraLexicalStore(client=_Client([])).get_graph_schema()


def test_router_selects_aurora_store_when_graph_backend_aurora(monkeypatch):
    from coa_ontology.inducer.routers.induce_unstructured import _build_lexical_store

    monkeypatch.setenv("GRAPH_BACKEND", "aurora")
    store = _build_lexical_store("g-abc", "us-east-1", {}, namespace="")
    assert isinstance(store, AuroraLexicalStore)
