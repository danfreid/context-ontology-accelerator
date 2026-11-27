# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AuroraGraphStore (and its traversal) against a real pgvector/PostgreSQL container."""

from __future__ import annotations

import pytest
import rdflib
from coa_ontology.catalog.turtle_export import format_grouped_turtle, turtle_has_triples
from coa_ontology.stores.aurora.graph import AuroraGraphStore
from coa_ontology.stores.neptune_db_graph import _SCL, OWL, RDFS, _ontology_graph_uri
from rdflib.compare import isomorphic

from . import scenario
from .parity_support import aurora_vector_store
from .scenario import NS, O1, O2, OTHER_NS, P

pytestmark = pytest.mark.integ

SUB = RDFS + "subClassOf"
OBJ_TTL = """
@prefix ex: <http://ex.org/t#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:A a owl:Class . ex:B a owl:Class ; rdfs:subClassOf ex:A . ex:C a owl:Class ; rdfs:subClassOf ex:B .
ex:D a owl:Class ; rdfs:subClassOf ex:C . ex:E a owl:Class ; rdfs:subClassOf ex:D .
ex:B2 a owl:Class ; rdfs:subClassOf ex:A .
ex:A rdfs:subClassOf ex:E .
ex:Free rdfs:subClassOf ex:A .
ex:rel a owl:ObjectProperty ; rdfs:domain ex:A ; rdfs:range ex:E .
ex:A ex:linksTo ex:C .
"""
T = "http://ex.org/t#"


@pytest.fixture
def graph(aurora_client):
    return AuroraGraphStore(namespace=NS, client=aurora_client, dimensions=scenario.DIMS)


@pytest.fixture
def ingested(graph):
    scenario.ingest_graph(graph, NS, other=False)
    return graph


# ── ingest and read back ────────────────────────────────────────────────


def test_named_graph_ingest_and_read_back(ingested):
    onto = ingested.get_ontology(O1)
    assert onto["title"] == "Finance" and sorted(onto["domain_tags"]) == ["bank", "fin"] and onto["imports"] == [O2]
    assert ingested.get_ontology_by_uri(O2)["license"] == "MIT"
    assert ingested.get_ontology("http://ex.org/missing") is None
    assert ingested.count_classes_and_properties(O1) == {"classes": 10, "properties": 3}
    assert ingested.count_classes_and_properties("http://ex.org/missing") == {"classes": 0, "properties": 0}


def test_ingest_is_idempotent_set_semantics(ingested, aurora_client):
    before = aurora_client.execute("SELECT count(*) AS n FROM coa_triple")[0]["n"]
    scenario.ingest_graph(ingested, NS, other=False)
    after = aurora_client.execute("SELECT count(*) AS n FROM coa_triple")[0]["n"]
    # Only new rows: 2 ontologies x (dct:created + dct:modified, new timestamps) and the 4 triples of the
    # blank-node restriction (fresh blank node ids on every load). Every other triple is deduplicated.
    assert after - before == 8


def test_update_ontology(ingested):
    updated = ingested.update_ontology(O1, {"status": "ready", "id": "ignored", "uri": "ignored"})
    assert updated["status"] == "ready" and updated["id"] == O1
    cleared = ingested.update_ontology(O1, {"status": None})
    assert "status" not in cleared
    assert ingested.update_ontology("http://ex.org/missing", {"a": "b"}) is None


def test_turtle_export_round_trip_and_grouped_formatting(graph):
    source = rdflib.Graph()
    source.parse(data=scenario.TURTLE_O1, format="turtle")
    graph.load_turtle(O1, scenario.TURTLE_O1)
    exported = graph.get_graph_turtle(O1)
    got = rdflib.Graph()
    got.parse(data=exported, format="turtle")
    assert isomorphic(source, got)
    assert any(isinstance(o, rdflib.Literal) and o.language == "fr" for _, _, o in got)
    assert any(o == rdflib.Literal("0", datatype=rdflib.XSD.nonNegativeInteger) for _, _, o in got)
    assert any(isinstance(s, rdflib.BNode) for s, _, _ in got)
    assert turtle_has_triples(exported)
    grouped = format_grouped_turtle(exported)
    regrouped = rdflib.Graph()
    regrouped.parse(data=grouped, format="turtle")
    assert isomorphic(source, regrouped)
    assert graph.get_graph_turtle("http://ex.org/none") is None


def test_load_turtle_returns_and_skips(graph):
    out = graph.load_turtle(O1, scenario.TURTLE_O2)
    assert out["graph_uri"] == _ontology_graph_uri(O1, NS) and out["bytes_loaded"] > 0
    assert graph.load_turtle(O1, "  ") == {"status": "skipped", "reason": "empty turtle"}
    with pytest.raises(Exception):  # noqa: B017 - rdflib's own syntax error type
        graph.load_turtle(O1, "this is not turtle")


def test_unusual_literals_survive(graph):
    graph.store_class(O1, P + "Odd", labels=['He said "hi"\nline2\ttab \\ back', "日本語", "x" * 20000], comments=["c"])
    ttl = rdflib.Graph()
    ttl.parse(data=graph.get_graph_turtle(O1), format="turtle")
    labels = {str(o) for _, p, o in ttl if str(p) == RDFS + "label"}
    assert 'He said "hi"\nline2\ttab \\ back' in labels and "日本語" in labels and "x" * 20000 in labels


def test_large_batch_spans_chunks(graph, rds_fake):
    classes = [
        {"class_uri": f"http://ex.org/big#C{i:05d}", "labels": [f"Class {i}"], "comments": ["d" * 600]}
        for i in range(4000)
    ]
    assert graph.store_classes_batch(O1, classes) == 4000
    assert graph.count_classes_and_properties(O1)["classes"] == 4000
    rds_fake.max_response_bytes = 100_000  # forces keyset pages to halve
    ov = graph.get_ontology_overview(O1, NS)
    assert len(ov["classes"]) == 4000
    ttl = rdflib.Graph()
    ttl.parse(data=graph.get_graph_turtle(O1), format="turtle")
    assert len(ttl) == 4000 * 4


# ── search across graphs and namespaces ─────────────────────────────────


def test_cross_graph_class_and_entity_search_without_namespace_leak(ingested, aurora_client):
    other = AuroraGraphStore(namespace=OTHER_NS, client=aurora_client, dimensions=scenario.DIMS)
    other.create_ontology({"uri": O1, "title": "Elsewhere"})
    other.store_classes_batch(
        O1, [{"class_uri": P + "Party", "labels": ["Foreign party"]}, {"class_uri": P + "Secret", "labels": ["S"]}]
    )

    hits = ingested.search_entities("party", NS, kind="class")
    by_uri = {h["uri"]: h for h in hits}
    assert set(by_uri) == {P + "Party"}
    party = by_uri[P + "Party"]
    assert sorted(party["graph_uris"]) == sorted([_ontology_graph_uri(O1, NS), _ontology_graph_uri(O2, NS)])
    assert party["label"] == "Client" and party["kind"] == "class"
    assert all(OTHER_NS not in g for h in hits for g in h["graph_uris"])
    assert ingested.search_entities("secret", NS) == []
    assert [h["uri"] for h in other.search_entities("secret", OTHER_NS)] == [P + "Secret"]
    assert other.search_entities("party", OTHER_NS)[0]["label"] == "Foreign party"
    assert ingested.count_entities("secret", NS) == 0


def test_count_equals_total_across_pages_and_pages_are_stable(ingested):
    total = ingested.count_entities("*", NS, kind="class")
    assert total > 10
    seen: list[str] = []
    for offset in range(0, total, 4):
        page = ingested.search_entities("*", NS, kind="class", limit=4, offset=offset)
        assert len(page) <= 4
        seen.extend(h["uri"] for h in page)
    assert len(seen) == total == len(set(seen))
    assert seen == [h["uri"] for h in ingested.search_entities("*", NS, kind="class", limit=500)]


def test_search_filters_and_hostile_input(ingested):
    assert {h["uri"] for h in ingested.search_entities("", NS, kind="datatype-property")} >= {P + "name", P + "balance"}
    assert all(h["kind"] == "class" for h in ingested.search_entities("*", NS, kind="class", limit=500))
    in_o2 = ingested.search_entities("*", NS, ontology_id=O2, limit=500)
    assert in_o2 and all(h["graph_uris"] == [_ontology_graph_uri(O2, NS)] for h in in_o2)
    no_o2 = ingested.search_entities("party", NS, exclude_ontology_id=O2)
    assert [h["graph_uris"] for h in no_o2 if h["uri"] == P + "Party"] == [[_ontology_graph_uri(O1, NS)]]
    for q in ("'; DROP TABLE coa_triple; --", "%", "_", "\\", "a" * 5000):
        ingested.search_entities(q, NS)
        ingested.count_entities(q, NS)
    assert ingested.count_classes_and_properties(O1)["classes"] == 10
    assert ingested.search_entities("%", NS) == []  # LIKE wildcards are plain characters here


def test_search_label_language_preference(ingested):
    from coa_common.label_language import LabelPreference

    plain = {h["uri"]: h["label"] for h in ingested.search_entities("compte", NS)}
    fr = {
        h["uri"]: h["label"]
        for h in ingested.search_entities("compte", NS, label_preference=LabelPreference(preferred_tag="fr"))
    }
    assert plain[P + "Account"] == "Account" and fr[P + "Account"] == "Compte"


# ── vertex, schema, overview ────────────────────────────────────────────


def test_get_vertex_spans_graphs_and_resolves_neighbors(ingested):
    v = ingested.get_vertex(P + "Party", NS)
    assert sorted(v["graph_uris"]) == sorted([_ontology_graph_uri(O1, NS), _ontology_graph_uri(O2, NS)])
    assert v["types"] == [OWL + "Class"]
    assert {"Client", "Customer", "Insured party"} == set(v["labels"])
    incoming = {e["neighbor"]["uri"]: e for e in v["edges"] if e["direction"] == "incoming" and e["predicate"] == SUB}
    assert incoming[P + "Person"]["neighbor"] == {"uri": P + "Person", "label": "Person", "kind": "class"}
    assert "http://ex.org/ins#Claim" in incoming  # a neighbor from the other graph
    outgoing = {e["neighbor"]["uri"]: e["neighbor"] for e in v["edges"] if e["direction"] == "outgoing"}
    assert outgoing[P + "Thing"]["label"] == "Thing"
    loan = ingested.get_vertex(P + "Loan", NS)
    assert loan["is_mapped"] is True
    assert ingested.get_vertex(P + "Nope", NS) is None
    assert ingested.get_vertex(P + "Party", OTHER_NS) is None


def test_namespace_schema_overview_and_mapped_lookups(ingested):
    schema = ingested.get_namespace_schema(NS, max_results=100)
    by = {c["uri"]: c for c in schema["classes"]}
    assert [c["uri"] for c in schema["classes"][:2]] == [P + "Account", P + "Loan"]  # mapped classes first
    assert {p["uri"] for p in by[P + "Party"]["properties"]} == {P + "name"}
    assert ingested.get_namespace_schema(NS, max_results=2)["classes"].__len__() == 2
    assert {c["uri"] for c in ingested.get_namespace_schema(NS, class_prefix="http://ex.org/ins")["classes"]} == {
        "http://ex.org/ins#Policy",
        "http://ex.org/ins#Claim",
    }
    ov = ingested.get_ontology_overview(O1, NS)
    assert {c["uri"] for c in ov["classes"]} >= {P + "Party", P + "Mortgage"}
    assert {p["uri"] for p in ov["datatype_properties"]} >= {P + "name", P + "code", P + "balance"}
    assert P + "owns" in {p["uri"] for p in ov["object_properties"]}
    assert P + "note" not in {p["uri"] for p in ov["object_properties"] + ov["datatype_properties"]}
    assert ingested.get_ontology_overview("http://ex.org/none", NS)["classes"] == []
    assert ingested.mapped_class_backends([P + "Account", P + "Loan", P + "Party"], NS) == {
        P + "Account": "SQL",
        P + "Loan": "RDF_SPARQL",
    }
    assert ingested.mapped_class_graph_sources([P + "Loan", P + "Account"], NS) == {P + "Loan": {"src1"}}


def test_sample_overview_is_bounded_with_subtree_sizes_from_the_traversal(graph):
    # 5 roots; the 3 with the biggest subtrees are kept, descendants capped at 50
    classes = [{"class_uri": f"http://ex.org/s#R{r}", "labels": [f"R{r}"]} for r in range(5)]
    for r in range(3):
        for i in range(30):
            classes.append(
                {
                    "class_uri": f"http://ex.org/s#R{r}_{i}",
                    "labels": [f"R{r} {i}"],
                    "super_classes": [f"http://ex.org/s#R{r}"],
                }
            )
    graph.store_classes_batch(O1, classes)
    ov = graph.get_ontology_overview(O1, NS, sample=True)
    uris = {c["uri"] for c in ov["classes"]}
    roots = {u for u in uris if "_" not in u}
    assert len(roots) == 3 and "http://ex.org/s#R3" not in uris and "http://ex.org/s#R4" not in uris
    assert len(uris) - 3 == 50
    assert ov["object_properties"] == [] and ov["datatype_properties"] == []
    assert all(set(c["sub_class_of"]) <= uris for c in ov["classes"])


def test_supersede_annotations_moves_or_drops(ingested):
    hist = _SCL + "supersededComment"
    n = ingested.supersede_annotations(
        O1, [P + "Party", P + "Mortgage", "bad uri"], {RDFS + "comment": hist, RDFS + "label": None}
    )
    assert n == 5  # Party: 2 labels + 1 comment, Mortgage: 1 label + 1 comment (O2 has its own graph)
    v = ingested.get_vertex(P + "Party", NS)
    assert v["comments"] == [] and v["labels"] == ["Insured party"]  # the O2 graph is not touched
    assert v["superseded"] == {"comments": ["A party to a contract"]}


# ── traversal ───────────────────────────────────────────────────────────


def _rows(rows):
    return [(r["uri"], r["depth"]) for r in rows]


def test_multi_hop_descendants_ancestors_and_depth_limit(graph):
    graph.load_turtle(O1, OBJ_TTL)
    down = graph.traverse([T + "A"], predicates=[SUB], direction="in", max_depth=10, ontology_id=O1)
    assert set(_rows(down)) >= {(T + "B", 1), (T + "B2", 1), (T + "Free", 1), (T + "C", 2), (T + "D", 3), (T + "E", 4)}
    assert all(r["start"] == T + "A" for r in down)
    one_hop = graph.traverse([T + "A"], predicates=[SUB], direction="in", max_depth=1)
    assert {r["uri"] for r in one_hop} == {T + "B", T + "B2", T + "Free"}
    assert max(r["depth"] for r in one_hop) == 1
    up = graph.traverse([T + "E"], predicates=[SUB], direction="out", max_depth=10)
    assert {r["uri"]: r["depth"] for r in up if r["uri"] in (T + "D", T + "C", T + "B", T + "A")} == {
        T + "D": 1,
        T + "C": 2,
        T + "B": 3,
        T + "A": 4,
    }
    both = graph.traverse([T + "C"], predicates=[SUB], direction="both", max_depth=1)
    assert {r["uri"] for r in both} == {T + "B", T + "D"}


def test_cycle_in_the_data_terminates_and_start_is_not_its_own_descendant(graph):
    graph.load_turtle(O1, OBJ_TTL)  # A subClassOf E ... subClassOf A is a 5-cycle
    rows = graph.traverse([T + "A"], predicates=[SUB], direction="out", max_depth=32)
    assert T + "A" not in {r["uri"] for r in rows}
    assert {r["uri"] for r in rows} == {T + "E", T + "D", T + "C", T + "B"}
    paths = graph.traverse([T + "A"], predicates=[SUB], direction="out", max_depth=32, return_paths=True)
    assert all(len(set(r["path"])) == len(r["path"]) for r in paths)  # no node repeats on a path
    assert max(len(r["path"]) for r in paths) == 5


def test_return_paths_predicate_filter_and_all_predicates(graph):
    graph.load_turtle(O1, OBJ_TTL)
    paths = graph.traverse([T + "E"], predicates=[SUB], direction="out", max_depth=3, return_paths=True)
    by_uri = {r["uri"]: r["path"] for r in paths}
    assert by_uri[T + "C"] == [T + "E", T + "D", T + "C"]
    anyp = {r["uri"] for r in graph.traverse([T + "A"], direction="out", max_depth=1)}
    assert T + "C" in anyp and T + "E" in anyp  # via ex:linksTo and subClassOf
    only_links = {r["uri"] for r in graph.traverse([T + "A"], predicates=[T + "linksTo"], direction="out", max_depth=1)}
    assert only_links == {T + "C"}
    literal_free = graph.traverse([T + "A"], direction="out", max_depth=1)
    assert all(r["uri"].startswith("http") for r in literal_free)  # literal objects are never followed


def test_traverse_scoping_limit_and_node_types(graph, aurora_client):
    graph.load_turtle(O1, OBJ_TTL)
    graph.load_turtle(
        O2,
        "<http://ex.org/t#Free> <"
        + SUB
        + "> <http://ex.org/t#Z> . <http://ex.org/t#Z> <"
        + SUB
        + "> <http://ex.org/t#Q> .",
    )
    scoped = {
        r["uri"] for r in graph.traverse([T + "Free"], predicates=[SUB], direction="out", max_depth=5, ontology_id=O1)
    }
    assert scoped == {T + "A", T + "E", T + "D", T + "C", T + "B"}
    both = {r["uri"] for r in graph.traverse([T + "Free"], predicates=[SUB], direction="out", max_depth=5)}
    assert {T + "Z", T + "Q"} <= both
    other_ns = AuroraGraphStore(namespace=OTHER_NS, client=aurora_client, dimensions=scenario.DIMS)
    assert other_ns.traverse([T + "Free"], predicates=[SUB], direction="out", max_depth=5) == []
    assert len(graph.traverse([T + "A"], direction="both", max_depth=5, limit=3)) == 3
    typed = graph.traverse([T + "A"], predicates=[SUB], direction="in", max_depth=10, node_types=[OWL + "Class"])
    assert T + "Free" not in {r["uri"] for r in typed} and T + "B" in {r["uri"] for r in typed}
    multi = graph.traverse([T + "E", T + "B2"], predicates=[SUB], direction="out", max_depth=1)
    assert {(r["start"], r["uri"]) for r in multi} == {(T + "E", T + "D"), (T + "B2", T + "A")}


def test_traversal_on_a_deep_chain_respects_the_depth_bound(graph):
    n = 40
    ttl = "".join(f"<http://ex.org/chain#N{i}> <{SUB}> <http://ex.org/chain#N{i + 1}> .\n" for i in range(n))
    graph.load_turtle(O1, ttl)
    rows = graph.traverse(["http://ex.org/chain#N0"], predicates=[SUB], direction="out", max_depth=25)
    assert len(rows) == 25 and max(r["depth"] for r in rows) == 25


# ── delete ──────────────────────────────────────────────────────────────


def test_delete_ontology_drops_only_its_graph_then_vector_teardown(ingested, aurora_client):
    vec = aurora_vector_store(aurora_client, NS, scenario.DIMS)
    vec.store_embeddings_batch(scenario.vector_items())
    before_o2 = ingested.get_graph_turtle(O2)
    # the order routers/ontologies.py _delete_ontology_backend uses: vectors first, then the graph
    assert vec.delete_embeddings_for_ontology(O1) == 5
    assert ingested.delete_ontology(O1) is True
    assert ingested.get_graph_turtle(O1) is None
    assert ingested.get_ontology(O1) is None
    assert ingested.search_entities("mortgage", NS) == []
    assert ingested.get_graph_turtle(O2) == before_o2
    assert {h["uri"] for h in ingested.search_entities("party", NS)} == {P + "Party"}
    assert [d["ontology_id"] for d in vec.list_embeddings_for_ontology(O2)] == [O2]
    assert ingested.delete_ontology(O1) is False
    assert vec.delete_index() is True
    assert ingested.get_ontology(O2) is not None  # the graph is independent of the vector index


def test_health_check(graph):
    assert graph.health_check() == {"status": "ok"}
