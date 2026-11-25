# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AuroraGraphStore: SQL shape, validation, batching, shapes (no database)."""

from __future__ import annotations

import json

import pytest
from coa_ontology.stores.aurora.graph import (
    MAX_TRAVERSAL_DEPTH,
    AuroraGraphStore,
    _rows_from_nt,
)
from coa_ontology.stores.aurora.schema import reset_schema_cache
from coa_ontology.stores.base import GraphStore
from coa_ontology.stores.neptune_db_graph import RDF, RDFS, _ontology_graph_uri

from .aurora_fakes import ScriptedRds, once_then, too_large

pytestmark = pytest.mark.unit

ONTO = "http://ex.org/onto"
A = "http://ex.org/A"
B = "http://ex.org/B"


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    reset_schema_cache()


def _store(rds: ScriptedRds, namespace: str = "ns1") -> AuroraGraphStore:
    return AuroraGraphStore(namespace=namespace, client=rds.client(), dimensions=8)


def _written(rds: ScriptedRds) -> list[dict]:
    rows: list[dict] = []
    for call in rds.sql_calls("INSERT INTO coa_triple"):
        if "rows" in call["params"]:
            rows.extend(json.loads(call["params"]["rows"]))
    return rows


def test_is_a_graph_store():
    assert isinstance(_store(ScriptedRds()), GraphStore)


def test_generated_triples_are_read_back_exactly():
    rows = _rows_from_nt(
        [
            '<http://ex/A> <http://ex/p> "line1\\nline2 \\"q\\" \\\\" .',
            '<http://ex/A> <http://ex/b> "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .',
            "<http://ex/A> <http://ex/c> <http://ex/B> .",
            '<http://ex/A> <http://ex/d> "plain"^^<http://www.w3.org/2001/XMLSchema#string> .',
        ]
    )
    assert rows[0]["o"] == 'line1\nline2 "q" \\' and rows[0]["o_kind"] == "literal"
    assert rows[1]["o_datatype"] == "http://www.w3.org/2001/XMLSchema#boolean"
    assert rows[2]["o_kind"] == "iri"
    assert rows[3]["o_datatype"] == ""  # xsd:string is the plain literal
    with pytest.raises(ValueError, match="cannot read"):
        _rows_from_nt(["garbage"])


def test_store_class_writes_one_insert_with_neptune_triple_set():
    rds = ScriptedRds()
    out = _store(rds).store_class(
        ONTO,
        A,
        labels=["Alpha"],
        comments=["The first"],
        super_classes=[B, "_:blank"],
        is_mapped=True,
        query_backend="RDF_SPARQL",
        data_source_id="src1",
    )
    assert out == {"uri": A, "ontology_id": ONTO}
    (call,) = rds.sql_calls("INSERT INTO coa_triple")
    assert call["params"]["g"] == _ontology_graph_uri(ONTO, "ns1")
    assert call["params"]["ns"] == "ns1"
    assert "http://ex.org" not in call["sql"]
    rows = _written(rds)
    preds = {r["p"] for r in rows}
    assert RDF + "type" in preds and RDFS + "subClassOf" in preds
    assert sum(r["p"] == RDFS + "subClassOf" for r in rows) == 1  # blank-node superclass skipped
    assert any(r["p"].endswith("isMapped") and r["o"] == "true" for r in rows)


def test_store_property_and_batches():
    rds = ScriptedRds()
    s = _store(rds)
    s.store_property(ONTO, "http://ex.org/p", label="p", domains=[A], ranges=[B])
    assert s.store_classes_batch(ONTO, [{"class_uri": A, "labels": ["a"]}, {"class_uri": B}]) == 2
    assert s.store_properties_batch(ONTO, [{"prop_uri": "http://ex.org/q", "label": "q"}]) == 1
    assert len(rds.sql_calls("INSERT INTO coa_triple")) == 3


def test_large_batches_span_chunks_inside_one_transaction():
    rds = ScriptedRds()
    classes = [{"class_uri": f"http://ex.org/C{i}", "comments": ["x" * 3000]} for i in range(900)]
    _store(rds).store_classes_batch(ONTO, classes)
    assert len(rds.sql_calls("INSERT INTO coa_triple")) >= 2
    assert rds.events == ["begin", "commit"]
    assert max(len(c["params"]["rows"].encode()) for c in rds.sql_calls("INSERT INTO coa_triple")) < 1_600_000


def test_invalid_iris_are_rejected_before_any_write():
    rds = ScriptedRds()
    with pytest.raises(ValueError):
        _store(rds).store_class(ONTO, "http://ex.org/bad> <x", labels=["x"])
    with pytest.raises(ValueError):
        _store(rds).create_ontology({"uri": "not a uri"})
    assert not rds.sql_calls("INSERT")


def test_get_ontology_by_uri_invalid_uri_is_none_without_query():
    rds = ScriptedRds()
    assert _store(rds).get_ontology_by_uri("nope") is None
    assert not rds.sql_calls("FROM coa_triple")


def test_get_ontology_by_uri_shapes_the_dict():
    rds = ScriptedRds()
    rows = [
        {"id": 1, "p": RDF + "type", "o": "http://www.w3.org/2002/07/owl#Ontology"},
        {"id": 2, "p": "http://purl.org/dc/terms/title", "o": "T"},
        {"id": 3, "p": "https://ontology-workbench.local/vocab#domainTag", "o": "fin"},
        {"id": 4, "p": "https://ontology-workbench.local/vocab#license", "o": "MIT"},
        {"id": 5, "p": "http://www.w3.org/2002/07/owl#imports", "o": B},
    ]
    rds.when("FROM coa_triple", rows)
    got = _store(rds).get_ontology_by_uri(ONTO)
    assert got == {
        "uri": ONTO,
        "id": ONTO,
        "domain_tags": ["fin"],
        "imports": [B],
        "title": "T",
        "license": "MIT",
    }
    assert _store(ScriptedRds()).get_ontology_by_uri(ONTO) is None


def test_update_ontology_missing_is_none_and_update_is_transactional():
    assert _store(ScriptedRds()).update_ontology(ONTO, {"title": "x"}) is None
    rds = ScriptedRds()
    rds.when("FROM coa_triple", [{"id": 1, "p": "http://purl.org/dc/terms/title", "o": "T"}])
    _store(rds).update_ontology(ONTO, {"status": "ready", "gone": None, "id": "ignored"})
    assert rds.events == ["begin", "commit"]
    delete = rds.sql_calls("DELETE FROM coa_triple")[0]
    assert json.loads(delete["params"]["preds"]) == [
        "https://ontology-workbench.local/vocab#status",
        "https://ontology-workbench.local/vocab#gone",
    ]
    (insert_call,) = rds.sql_calls("INSERT INTO coa_triple")
    assert [r["o"] for r in json.loads(insert_call["params"]["rows"])] == ["ready"]


def test_delete_ontology_reports_existence_and_drops_in_batches():
    rds = ScriptedRds()
    rds.when("FROM coa_triple WHERE graph_uri = :g AND s = :s", [{"id": 1, "p": RDF + "type", "o": "x"}])
    counts = [20000, 5]
    rds.when("DELETE FROM coa_triple", lambda sql, p: [{"n": counts.pop(0)}])
    assert _store(rds).delete_ontology(ONTO) is True
    assert len(rds.sql_calls("DELETE FROM coa_triple")) == 2
    rds2 = ScriptedRds()
    rds2.when("DELETE FROM coa_triple", [{"n": 0}])
    assert _store(rds2).delete_ontology(ONTO) is False


def test_load_turtle_skips_empty_and_inserts_parsed_triples():
    rds = ScriptedRds()
    s = _store(rds)
    assert s.load_turtle(ONTO, "  \n") == {"status": "skipped", "reason": "empty turtle"}
    assert not rds.calls
    ttl = '@prefix ex: <http://ex.org/> .\nex:A a ex:T ; ex:p "v"@en , 3 ; ex:q [ ex:r ex:B ] .\n'
    out = s.load_turtle(ONTO, ttl)
    assert out["graph_uri"] == _ontology_graph_uri(ONTO, "ns1")
    assert out["bytes_loaded"] == len(ttl.encode())
    rows = _written(rds)
    assert len(rows) == 5
    assert {r["o_kind"] for r in rows} == {"iri", "literal", "bnode"}
    assert any(r["o_lang"] == "en" for r in rows)
    assert any(r["o_datatype"].endswith("#integer") for r in rows)


def test_load_turtle_honours_explicit_graph_uri():
    rds = ScriptedRds()
    _store(rds).load_turtle(ONTO, "<http://a> <http://b> <http://c> .", graph_uri="http://g/x")
    assert rds.sql_calls("INSERT INTO coa_triple")[0]["params"]["g"] == "http://g/x"


def test_get_graph_turtle_none_when_empty_and_serialises_rows():
    assert _store(ScriptedRds()).get_graph_turtle(ONTO) is None
    rds = ScriptedRds()
    rds.when(
        "FROM coa_triple",
        [
            {
                "id": 1,
                "s": A,
                "s_kind": "iri",
                "p": RDFS + "label",
                "o": "Alpha",
                "o_kind": "literal",
                "o_lang": "en",
                "o_datatype": "",
            },
            {
                "id": 2,
                "s": "b0",
                "s_kind": "bnode",
                "p": RDFS + "seeAlso",
                "o": B,
                "o_kind": "iri",
                "o_lang": "",
                "o_datatype": "",
            },
        ],
    )
    ttl = _store(rds).get_graph_turtle(ONTO)
    assert '"Alpha"@en' in ttl and "seeAlso" in ttl


def test_supersede_is_one_statement_per_chunk_and_skips_empty_input():
    rds = ScriptedRds()
    s = _store(rds)
    assert s.supersede_annotations(ONTO, [], {RDFS + "comment": None}) == 0
    assert s.supersede_annotations(ONTO, [A], {}) == 0
    assert s.supersede_annotations(ONTO, ["bad uri"], {RDFS + "comment": None}) == 0
    assert not rds.calls
    rds.when("gone AS", [{"n": 3}])
    subjects = [f"http://ex.org/S{i}" for i in range(450)]
    assert s.supersede_annotations(ONTO, subjects, {RDFS + "comment": "http://h/old", "http://x/p": None}) == 9
    calls = rds.sql_calls("gone AS")
    assert len(calls) == 3
    assert json.loads(calls[0]["params"]["pairs"]) == [
        {"live": RDFS + "comment", "hist": "http://h/old"},
        {"live": "http://x/p", "hist": None},
    ]
    assert len(json.loads(calls[0]["params"]["subjects"])) == 200


def test_count_classes_and_properties_shape_and_failure_is_none():
    rds = ScriptedRds()
    rds.when("count(DISTINCT s)", [{"classes": 4, "props": 7}])
    assert _store(rds).count_classes_and_properties(ONTO) == {"classes": 4, "properties": 7}
    rds2 = ScriptedRds()
    rds2.when("count(DISTINCT s)", RuntimeError("db down"))
    assert _store(rds2).count_classes_and_properties(ONTO) is None


def test_get_vertex_shape_and_blank_node_incoming_skipped():
    rds = ScriptedRds()
    out_rows = [
        {
            "id": 1,
            "graph_uri": "g1",
            "p": RDF + "type",
            "o": "http://www.w3.org/2002/07/owl#Class",
            "o_kind": "iri",
            "o_lang": "",
        },
        {"id": 2, "graph_uri": "g1", "p": RDFS + "label", "o": "Alpha", "o_kind": "literal", "o_lang": ""},
        {"id": 3, "graph_uri": "g1", "p": RDFS + "subClassOf", "o": B, "o_kind": "iri", "o_lang": ""},
        {"id": 4, "graph_uri": "g1", "p": "http://ex.org/note", "o": "free text", "o_kind": "literal", "o_lang": ""},
        {"id": 5, "graph_uri": "g1", "p": "http://ex.org/r", "o": "b1", "o_kind": "bnode", "o_lang": ""},
    ]
    in_rows = [
        {"id": 9, "graph_uri": "g2", "p": RDFS + "subClassOf", "o": "http://ex.org/C", "o_kind": "iri", "o_lang": ""},
        {"id": 10, "graph_uri": "g2", "p": "http://ex.org/first", "o": "b9", "o_kind": "bnode", "o_lang": ""},
    ]
    rds.when(
        "AND s IN",
        [
            {"id": 1, "s": B, "p": RDFS + "label", "o": "Beta", "o_lang": ""},
            {"id": 2, "s": B, "p": RDF + "type", "o": "http://www.w3.org/2002/07/owl#Class", "o_lang": ""},
        ],
    )
    rds.when("AND o = :v", in_rows)
    rds.when("AND s = :v", out_rows)
    v = _store(rds).get_vertex(A, "ns1")
    assert v["graph_uris"] == ["g1", "g2"]
    assert v["types"] == ["http://www.w3.org/2002/07/owl#Class"]
    assert v["labels"] == ["Alpha"]
    kinds = {(e["direction"], e["neighbor"]["uri"]): e["neighbor"]["kind"] for e in v["edges"]}
    assert kinds[("outgoing", B)] == "class"
    assert kinds[("outgoing", "free text")] == "literal"
    assert kinds[("outgoing", "b1")] == "blank-node"
    assert kinds[("incoming", "http://ex.org/C")] == "unknown"
    assert ("incoming", "b9") not in kinds
    beta = next(e for e in v["edges"] if e["neighbor"]["uri"] == B)
    assert beta["neighbor"]["label"] == "Beta"
    assert beta["predicate_label"] == "subClassOf"


def test_get_vertex_none_when_nothing_found_and_invalid_uri_raises():
    assert _store(ScriptedRds()).get_vertex(A, "ns1") is None
    with pytest.raises(ValueError):
        _store(ScriptedRds()).get_vertex("not a uri")


def test_search_clamps_limit_and_offset_and_parameterises_the_query():
    rds = ScriptedRds()
    _store(rds).search_entities("Cust'omer; DROP", namespace="ns1", limit=100000, offset=-5)
    call = rds.sql_calls("pg AS")[0]
    assert call["params"]["limit"] == 500
    assert call["params"]["offset"] == 0
    assert call["params"]["q"] == "cust'omer; drop"
    assert "DROP" not in call["sql"]


def test_search_wildcard_has_no_text_filter_and_unknown_kind_is_ignored():
    rds = ScriptedRds()
    s = _store(rds)
    s.search_entities("*", namespace="ns1", kind="mystery")
    sql = rds.sql_calls("pg AS")[0]["sql"]
    assert "strpos" not in sql and ":kinds" not in sql
    s.search_entities("x", namespace="ns1", kind="class", ontology_id="o1", exclude_ontology_id="o2")
    call = rds.sql_calls("pg AS")[1]
    assert "t.graph_uri = :g" in call["sql"] and "t.graph_uri <> :xg" in call["sql"]
    assert json.loads(call["params"]["kinds"]) == [
        "http://www.w3.org/2002/07/owl#Class",
        "http://www.w3.org/2000/01/rdf-schema#Class",
    ]
    assert call["params"]["g"] == _ontology_graph_uri("o1", "ns1")


def test_search_result_shape_dedup_and_label_choice():
    rds = ScriptedRds()
    owl = "http://www.w3.org/2002/07/owl#"
    rds.when(
        "pg AS",
        [
            {"s": A, "sort_key": "alpha", "g": "g1", "label": "Alpha", "label_lang": "", "t": RDF + "Property"},
            {"s": A, "sort_key": "alpha", "g": "g1", "label": "Alpha", "label_lang": "", "t": owl + "Class"},
            {"s": A, "sort_key": "alpha", "g": "g2", "label": None, "label_lang": None, "t": None},
            {"s": B, "sort_key": "b", "g": "g1", "label": None, "label_lang": None, "t": None},
        ],
    )
    hits = _store(rds).search_entities("a", namespace="ns1")
    assert hits == [
        {"uri": A, "label": "Alpha", "kind": "class", "graph_uris": ["g1", "g2"]},
        {"uri": B, "label": None, "kind": "unknown", "graph_uris": ["g1"]},
    ]


def test_search_splits_the_page_when_the_response_is_too_large():
    rds = ScriptedRds()

    def row(n):
        return {"s": f"http://ex.org/{n}", "sort_key": str(n), "g": "g", "label": None, "label_lang": None, "t": None}

    rds.when("pg AS", once_then(too_large(), lambda sql, p: [row(p["offset"] + i) for i in range(p["limit"])]))
    hits = _store(rds).search_entities("*", namespace="ns1", limit=10)
    assert [h["uri"] for h in hits] == [f"http://ex.org/{i}" for i in range(10)]
    calls = rds.sql_calls("pg AS")
    assert [(c["params"]["offset"], c["params"]["limit"]) for c in calls] == [(0, 10), (0, 5), (5, 5)]


def test_count_entities():
    rds = ScriptedRds()
    rds.when("count(*) AS total", [{"total": 12}])
    assert _store(rds).count_entities("x", namespace="ns1") == 12
    assert _store(ScriptedRds()).count_entities("x", namespace="ns1") == 0


def test_mapped_class_lookups():
    rds = ScriptedRds()
    rds.when("SELECT DISTINCT m.s", [{"c": A, "backend": None}, {"c": B, "backend": "RDF_SPARQL"}])
    rds.when("SELECT DISTINCT q.s", [{"c": B, "src": "s1"}, {"c": B, "src": "s2"}])
    s = _store(rds)
    assert s.mapped_class_backends([A, B, "bad uri"], "ns1") == {A: "SQL", B: "RDF_SPARQL"}
    assert s.mapped_class_graph_sources([B], "ns1") == {B: {"s1", "s2"}}
    assert s.mapped_class_backends([]) == {}
    assert s.mapped_class_graph_sources(["bad uri"]) == {}


def test_namespace_schema_validates_the_namespace_first():
    with pytest.raises(ValueError):
        _store(ScriptedRds()).get_namespace_schema("bad ns!")


def test_namespace_schema_class_prefix_variants():
    rds = ScriptedRds()
    s = _store(rds)
    s.get_namespace_schema("ns1", class_prefix="http://ex.org/ins")
    s.get_namespace_schema("ns1", class_prefix="http://ex.org/ins#")
    first, second = rds.sql_calls("MIN(CASE WHEN m.o")
    assert first["params"]["pfx_hash"] == "http://ex.org/ins#" and first["params"]["pfx_slash"] == "http://ex.org/ins/"
    assert "pfx_hash" not in second["params"] and "starts_with(t.s, :pfx)" in second["sql"]


def test_traverse_validates_arguments():
    s = _store(ScriptedRds())
    with pytest.raises(ValueError, match="max_depth"):
        s.traverse([A], max_depth=0)
    with pytest.raises(ValueError, match="max_depth"):
        s.traverse([A], max_depth=MAX_TRAVERSAL_DEPTH + 1)
    with pytest.raises(ValueError, match="direction"):
        s.traverse([A], max_depth=2, direction="sideways")
    with pytest.raises(ValueError):
        s.traverse(["bad uri"], max_depth=2)
    assert s.traverse([], max_depth=2) == []


def test_traverse_is_one_recursive_statement_with_bound_depth_and_cycle_guard():
    rds = ScriptedRds()
    rds.when("WITH RECURSIVE", [{"start": A, "uri": B, "depth": 1}])
    s = _store(rds)
    rows = s.traverse([A], predicates=[RDFS + "subClassOf"], direction="in", max_depth=3, ontology_id="o1")
    assert rows == [{"start": A, "uri": B, "depth": 1}]
    (call,) = rds.sql_calls("WITH RECURSIVE")
    assert call["params"]["max_depth"] == 3
    assert "w.depth < :max_depth" in call["sql"]
    assert " UNION SELECT" in call["sql"]  # set semantics, no path column
    assert "path" not in call["sql"]
    assert "e.o = w.uri" in call["sql"] and "e.s = w.uri" not in call["sql"]
    assert "e.graph_uri = :g" in call["sql"]
    assert json.loads(call["params"]["preds"]) == [RDFS + "subClassOf"]
    assert A not in call["sql"]
    assert call["params"]["limit"] == 1000


def test_traverse_paths_use_a_visited_array_and_parse_it():
    rds = ScriptedRds()
    rds.when("WITH RECURSIVE", [{"start": A, "uri": B, "depth": 1, "path": json.dumps([A, B])}])
    rows = _store(rds).traverse([A], max_depth=2, direction="both", return_paths=True, limit=10**6)
    assert rows[0]["path"] == [A, B]
    call = rds.sql_calls("WITH RECURSIVE")[0]
    assert "n.nxt <> ALL (w.path)" in call["sql"]
    assert "e.s = w.uri" in call["sql"] and "e.o = w.uri" in call["sql"]
    assert call["params"]["limit"] == 5000


def test_sample_overview_uses_a_traversal_for_subtree_sizes():
    rds = ScriptedRds()
    cls = "http://www.w3.org/2002/07/owl#Class"
    rows = [
        {"id": i, "s": u, "p": RDF + "type", "o": cls, "o_kind": "iri", "o_lang": ""} for i, u in enumerate([A, B], 1)
    ]
    rows.append({"id": 3, "s": B, "p": RDFS + "subClassOf", "o": A, "o_kind": "iri", "o_lang": ""})
    rds.when("t.id > :after", once_then(rows, []))
    rds.when("WITH RECURSIVE", [{"start": A, "n": 1}])
    out = _store(rds).get_ontology_overview("o1", "ns1", sample=True)
    assert [c["uri"] for c in out["classes"]] == [A, B]
    assert out["classes"][1]["sub_class_of"] == [A]
    assert out["object_properties"] == [] and out["datatype_properties"] == []
    walk = rds.sql_calls("WITH RECURSIVE")[0]
    assert "ty.p = :ntype" in walk["sql"] and "e.graph_uri = :g" in walk["sql"]


def test_health_check_ok():
    assert _store(ScriptedRds()).health_check() == {"status": "ok"}
