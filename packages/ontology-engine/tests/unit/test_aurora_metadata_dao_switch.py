# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Namespace and source lookups with ``METADATA_BACKEND=aurora`` over the in-memory Aurora metadata store."""

from __future__ import annotations

import pytest
from coa_common.testing_aurora import install

pytestmark = pytest.mark.unit

NS = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def store(monkeypatch):
    s = install(monkeypatch)
    monkeypatch.setenv("NAMESPACES_TABLE", "coa-dev-namespaces")
    monkeypatch.setenv("SOURCES_TABLE", "coa-dev-sources")
    s["coa-dev-namespaces"] = {
        (f"NS#{NS}", "METADATA"): {"PK": f"NS#{NS}", "SK": "METADATA", "name": "sales", "language": "de"},
        ("NS_NAME#sales", "RESERVATION"): {"PK": "NS_NAME#sales", "SK": "RESERVATION", "namespaceId": NS},
    }
    s["coa-dev-sources"] = {
        (f"NS#{NS}", "SRC#a"): {
            "PK": f"NS#{NS}",
            "SK": "SRC#a",
            "sourceId": "a",
            "name": "graph-a",
            "sourceSubType": "RDF_SPARQL",
            "status": "APPROVED",
            "ignored": "x",
        },
        (f"NS#{NS}", "OTHER"): {"PK": f"NS#{NS}", "SK": "OTHER"},
    }
    return s


def test_namespace_language_resolves_a_name_through_the_reservation(store):
    from coa_ontology.catalog import namespace_language

    namespace_language._language_cache.clear()
    assert namespace_language.namespace_language("sales") == "de"


def test_graph_sources_are_listed_with_a_string_key_condition(store):
    from coa_ontology.catalog import graph_source_resolver

    items = graph_source_resolver._namespace_sources(NS)
    assert [i["sourceId"] for i in items] == ["a"]
    assert items[0] == {"sourceId": "a", "name": "graph-a", "sourceSubType": "RDF_SPARQL", "status": "APPROVED"}
