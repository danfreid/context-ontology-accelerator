# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Behavioural parity: the Aurora pair must answer like the existing Neptune DB + OpenSearch pair.

The reference side runs the real ``NeptuneDBGraphStore`` (its SPARQL evaluated by rdflib over an
in-memory dataset) and the real ``OpenSearchVectorStore`` (over a brute-force cosine fake). The same
scenario runs on both and the normalised results are compared operation by operation.
"""

from __future__ import annotations

import pytest
from coa_ontology.stores.aurora.graph import AuroraGraphStore
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore

from . import scenario
from .parity_support import FakeAoss, aurora_vector_store, patch_neptune_with_rdflib

pytestmark = pytest.mark.integ

NS = scenario.NS


@pytest.fixture
def neptune(monkeypatch):
    patch_neptune_with_rdflib(monkeypatch)
    return NeptuneDBGraphStore(namespace=NS)


@pytest.fixture
def aurora_graph(aurora_client):
    return AuroraGraphStore(namespace=NS, client=aurora_client, dimensions=scenario.DIMS)


@pytest.fixture
def opensearch(monkeypatch):
    from coa_ontology.stores import opensearch_vector as mod

    fake = FakeAoss()
    monkeypatch.setattr(mod, "OSS_ENDPOINT", "http://aoss.invalid")
    monkeypatch.setattr(mod, "OSS_DIMENSIONS", scenario.DIMS)
    monkeypatch.setattr(mod, "AossVectorClient", lambda **kw: fake)
    return mod.OpenSearchVectorStore(namespace=NS)


@pytest.fixture
def aurora_vector(aurora_client):
    return aurora_vector_store(aurora_client, NS, scenario.DIMS)


def _diff(ref: dict, got: dict) -> list[str]:
    problems = []
    assert ref.keys() == got.keys()
    for key in ref:
        if ref[key] != got[key]:
            problems.append(f"{key}:\n{_describe(ref[key], got[key])}")
    return problems


def _short(value, limit: int = 700) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def _describe(ref, got, indent: str = "  ") -> str:
    """Name what differs: first differing elements of lists, differing keys of dicts, else both values."""
    if isinstance(ref, list) and isinstance(got, list):
        if len(ref) == len(got):
            pairs = [(i, a, b) for i, (a, b) in enumerate(zip(ref, got, strict=True)) if a != b]
            if all(isinstance(a, dict) for _, a, _ in pairs):
                return "\n".join(f"{indent}[{i}]\n{_describe(a, b, indent + '  ')}" for i, a, b in pairs[:3])
            if sorted(map(repr, ref)) == sorted(map(repr, got)):
                return f"{indent}same items, different order"
        only_ref = [x for x in ref if x not in got]
        only_got = [x for x in got if x not in ref]
        return f"{indent}only neptune: {_short(only_ref)}\n{indent}only aurora:  {_short(only_got)}"
    if isinstance(ref, dict) and isinstance(got, dict):
        keys = sorted(k for k in ref.keys() | got.keys() if ref.get(k) != got.get(k))
        return "\n".join(f"{indent}[{k}]\n{_describe(ref.get(k), got.get(k), indent + '  ')}" for k in keys[:4])
    return f"{indent}neptune: {_short(ref)}\n{indent}aurora:  {_short(got)}"


def test_graph_results_match_neptune(neptune, aurora_graph):
    scenario.ingest_graph(neptune, NS)
    scenario.ingest_graph(aurora_graph, NS)
    # a second namespace holds an ontology with the same id: it must not leak into NS results
    ref = scenario.graph_results(neptune, NS)
    got = scenario.graph_results(aurora_graph, NS)
    problems = _diff(ref, got)
    assert not problems, "\n".join(problems)


def test_search_without_matches_is_empty(aurora_graph):
    """Recorded gap: rdflib cannot be the reference for this one operation.

    Neptune's paged search wraps a ``GROUP BY`` sub-select. With zero matching rows rdflib emits one
    unbound group, which then joins to every triple, so rdflib returns the whole namespace where a real
    SPARQL engine returns nothing (the Neptune store already guards the same artifact with ``HAVING`` in
    ``get_namespace_schema``). The expected answer, an empty page and a zero count, is asserted directly.
    """
    scenario.ingest_graph(aurora_graph, NS)
    assert aurora_graph.search_entities("zzz-no-such-thing", NS) == []
    assert aurora_graph.count_entities("zzz-no-such-thing", NS) == 0


def test_turtle_export_is_isomorphic_to_neptune(neptune, aurora_graph):
    scenario.ingest_graph(neptune, NS, other=False)
    scenario.ingest_graph(aurora_graph, NS, other=False)
    for onto in (scenario.O1, scenario.O2):
        ref = scenario.turtle_graph(neptune.get_graph_turtle(onto))
        got = scenario.turtle_graph(aurora_graph.get_graph_turtle(onto))
        assert ref is not None and got is not None
        # Neptune keeps load_turtle bnodes as written; compare up to blank node renaming.
        from rdflib.compare import isomorphic

        assert isomorphic(ref, got), onto
    assert neptune.get_graph_turtle("http://ex.org/none") is None
    assert aurora_graph.get_graph_turtle("http://ex.org/none") is None


def test_supersede_matches_neptune(neptune, aurora_graph):
    from coa_ontology.stores.neptune_db_graph import _SCL, RDFS

    def run(store):
        scenario.ingest_graph(store, NS, other=False)
        n = store.supersede_annotations(
            scenario.O1,
            [scenario.P + "Party", scenario.P + "Person", "bad uri"],
            {RDFS + "comment": _SCL + "supersededComment", RDFS + "label": None},
        )
        ttl = scenario.turtle_graph(store.get_graph_turtle(scenario.O1))
        return n, ttl

    ref_n, ref_g = run(neptune)
    got_n, got_g = run(aurora_graph)
    assert ref_n == got_n > 0
    from rdflib.compare import isomorphic

    assert isomorphic(ref_g, got_g)


def test_delete_ontology_matches_neptune(neptune, aurora_graph):
    for store in (neptune, aurora_graph):
        scenario.ingest_graph(store, NS, other=False)
    assert neptune.delete_ontology(scenario.O1) is aurora_graph.delete_ontology(scenario.O1) is True
    assert neptune.delete_ontology(scenario.O1) is aurora_graph.delete_ontology(scenario.O1) is False
    assert neptune.get_graph_turtle(scenario.O1) is None
    assert aurora_graph.get_graph_turtle(scenario.O1) is None
    for store in (neptune, aurora_graph):
        assert store.get_ontology(scenario.O2) is not None
        assert store.get_ontology_overview(scenario.O1, NS)["classes"] == []
        assert store.search_entities("*", NS, ontology_id=scenario.O1) == []


def test_vector_results_match_opensearch(opensearch, aurora_vector):
    ref = scenario.vector_results(opensearch, NS)
    got = scenario.vector_results(aurora_vector, NS)
    ref_scores = ref.pop("nearest scores")
    got_scores = got.pop("nearest scores")
    problems = _diff(ref, got)
    assert not problems, "\n".join(problems)
    # OpenSearch cosinesimil scores are (1 + cos) / 2; Aurora returns raw cosine (the VectorStore contract).
    assert len(ref_scores) == len(got_scores) > 0
    for s_os, s_aurora in zip(ref_scores, got_scores, strict=True):
        assert -1.0 <= s_aurora <= 1.0
        assert s_os == pytest.approx((1 + s_aurora) / 2, abs=1e-6)


def test_intentional_difference_upsert_replaces_where_opensearch_appends(opensearch, aurora_vector):
    item = scenario.vector_items()[0]
    for store in (opensearch, aurora_vector):
        store.store_embedding(item)
        store.store_embedding({**item, "text": "again"})
    assert len(opensearch.get_embeddings_for_entity(item["entity_uri"])) == 2  # AOSS cannot upsert
    docs = aurora_vector.get_embeddings_for_entity(item["entity_uri"])
    assert [d["text"] for d in docs] == ["again"]
