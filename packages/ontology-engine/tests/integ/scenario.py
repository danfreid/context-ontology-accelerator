# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One data set and one list of read operations, run unchanged against any GraphStore / VectorStore.

The graph and vector parity tests run :func:`ingest_graph` + :func:`graph_results` on the Neptune
reference (rdflib behind the SPARQL transport) and on the Aurora store, then compare the normalised
results operation by operation.
"""

from __future__ import annotations

import math
import re
from typing import Any

import rdflib
from coa_common.constants import VOCAB_URI
from coa_common.label_language import LabelPreference

NS = "parity"
OTHER_NS = "elsewhere"
O1 = "http://ex.org/fin"
O2 = "http://ex.org/ins"
P = "http://ex.org/fin#"
SCL = VOCAB_URI

TURTLE_O1 = f"""
@prefix ex: <{P}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix scl: <{SCL}> .

ex:name a owl:DatatypeProperty ; rdfs:label "name" , "nom"@fr ; rdfs:comment "Legal name" ;
    rdfs:domain ex:Party ; rdfs:range xsd:string .
ex:balance a owl:DatatypeProperty ; rdfs:label "balance" ; rdfs:domain ex:Account ; rdfs:range xsd:decimal .
ex:owns a owl:ObjectProperty ; rdfs:label "owns" ; rdfs:domain ex:Party ; rdfs:range ex:Account .
ex:note a owl:AnnotationProperty ; rdfs:label "note" .
ex:Instrument rdfs:subClassOf [ a owl:Restriction ; owl:onProperty ex:owns ;
    owl:minCardinality "0"^^xsd:nonNegativeInteger ] .
ex:Account rdfs:label "Compte"@fr .
ex:Person skos:exactMatch <http://schema.org/Person> .
ex:Org scl:groundedTo <http://other.example/Organization> ; skos:closeMatch <http://other.example/Org2> .
ex:Event skos:altLabel "Occurrence"@en ; rdfs:seeAlso ex:Person .
"""

TURTLE_O2 = f"""
@prefix ex: <{P}> .
@prefix ins: <http://ex.org/ins#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ins:Policy a owl:Class ; rdfs:label "Policy" ; rdfs:subClassOf ex:Thing .
ins:premium a owl:DatatypeProperty ; rdfs:label "premium" ; rdfs:domain ins:Policy ;
    rdfs:range <http://www.w3.org/2001/XMLSchema#decimal> .
"""


def _classes(prefix: str, spec: list[tuple]) -> list[dict]:
    out = []
    for name, label, parents, extra in spec:
        item = {
            "class_uri": prefix + name,
            "labels": label,
            "super_classes": [p if p.startswith("_:") else prefix + p for p in parents],
        }
        item.update(extra)
        out.append(item)
    return out


O1_CLASSES = _classes(
    P,
    [
        ("Thing", ["Thing"], [], {}),
        ("Party", ["Customer", "Client"], ["Thing"], {"comments": ["A party to a contract"]}),
        ("Person", ["Person"], ["Party"], {}),
        ("Org", ["Organization"], ["Party", "_:blank"], {}),
        ("Account", ["Account"], [], {"is_mapped": True}),
        ("Loan", ["Loan"], ["Account"], {"is_mapped": True, "query_backend": "RDF_SPARQL", "data_source_id": "src1"}),
        ("Mortgage", ["Mortgage"], ["Loan"], {"comments": ["Secured by property"]}),
        ("Instrument", ["Instrument"], [], {}),
        ("Event", ["Event"], [], {}),
        ("Zone", [], [], {}),
    ],
)
O2_CLASSES = [
    {"class_uri": P + "Party", "labels": ["Insured party"], "super_classes": []},
    {"class_uri": "http://ex.org/ins#Claim", "labels": ["Claim"], "super_classes": [P + "Party"], "comments": ["c"]},
]


def ingest_graph(graph: Any, namespace: str, *, other: bool = True) -> None:
    """Write the same ontologies through the public GraphStore surface."""
    graph.create_ontology(
        {"uri": O1, "title": "Finance", "description": "d", "domain_tags": ["fin", "bank"], "imports": [O2]}
    )
    graph.create_ontology({"uri": O2, "title": "Insurance", "license": "MIT"})
    graph.store_classes_batch(O1, O1_CLASSES)
    graph.store_classes_batch(O2, O2_CLASSES)
    graph.store_property(
        O1,
        P + "code",
        label="code",
        comment="c",
        domains=[P + "Party"],
        ranges=["http://www.w3.org/2001/XMLSchema#string"],
    )
    graph.store_properties_batch(
        O1, [{"prop_uri": P + "holds", "label": "holds", "domains": [P + "Party"], "ranges": [P + "Account"]}]
    )
    graph.load_turtle(O1, TURTLE_O1)
    graph.load_turtle(O2, TURTLE_O2)
    if other:
        graph.create_ontology({"uri": O1, "title": "Leak"})
        graph.store_classes_batch(O1, [{"class_uri": P + "Leaky", "labels": ["Leaky"], "super_classes": [P + "Thing"]}])


def _sorted_edges(vertex: dict | None) -> dict | None:
    if vertex is None:
        return None
    out = dict(vertex)
    out["edges"] = sorted(
        out["edges"], key=lambda e: (e["direction"], e["predicate"], e["neighbor"]["uri"], e["neighbor"]["kind"])
    )
    return out


_BNODE = re.compile(r"n[0-9a-f]{32}b\d+")


def norm(value: Any) -> Any:
    """Replace blank node ids (random per parse) with a fixed marker, recursively."""
    if isinstance(value, str):
        return "_:bnode" if _BNODE.fullmatch(value) else value
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(norm(v) for v in value)
    return value


def _hits(hits: list[dict]) -> list[tuple]:
    """Normalised hits; blank node subjects are left out (their ids and sort position are random)."""
    return [
        (h["uri"], h["label"], h["kind"], tuple(sorted(h["graph_uris"])))
        for h in hits
        if not _BNODE.fullmatch(h["uri"])
    ]


def _overview(ov: dict | None) -> dict | None:
    if ov is None:
        return None

    def cls(c: dict) -> dict:
        return {**c, "sub_class_of": sorted(c["sub_class_of"])}

    return {
        "ontology_id": ov["ontology_id"],
        "graph_uri": ov["graph_uri"],
        "classes": [cls(c) for c in ov["classes"]],
        "object_properties": ov["object_properties"],
        "datatype_properties": ov["datatype_properties"],
    }


def _ontology(d: dict | None) -> dict | None:
    if d is None:
        return None
    d = {k: v for k, v in d.items() if k not in {"created", "modified"}}
    d["domain_tags"] = sorted(d["domain_tags"])
    d["imports"] = sorted(d["imports"])
    return d


def graph_results(graph: Any, namespace: str) -> dict[str, Any]:
    """Run every read operation and return normalised results keyed by operation."""
    fr = LabelPreference(preferred_tag="fr")
    out: dict[str, Any] = {}
    out["get_ontology o1"] = _ontology(graph.get_ontology(O1))
    out["get_ontology o2"] = _ontology(graph.get_ontology(O2))
    out["get_ontology missing"] = graph.get_ontology("http://ex.org/none")
    out["update_ontology"] = _ontology(graph.update_ontology(O2, {"status": "ready", "license": None}))
    out["update_ontology missing"] = graph.update_ontology("http://ex.org/none", {"status": "x"})
    out["counts o1"] = graph.count_classes_and_properties(O1)
    out["counts o2"] = graph.count_classes_and_properties(O2)
    out["search all"] = _hits(graph.search_entities("*", namespace, limit=500))
    out["count all"] = graph.count_entities("*", namespace)
    out["search party"] = _hits(graph.search_entities("party", namespace))
    out["count party"] = graph.count_entities("party", namespace)
    out["search label"] = _hits(graph.search_entities("custom", namespace))
    out["search class"] = _hits(graph.search_entities("*", namespace, kind="class", limit=500))
    out["count class"] = graph.count_entities("*", namespace, kind="class")
    out["search dtprop"] = _hits(graph.search_entities("", namespace, kind="datatype-property"))
    out["search objprop"] = _hits(graph.search_entities("o", namespace, kind="object-property"))
    out["search o2 only"] = _hits(graph.search_entities("*", namespace, ontology_id=O2, limit=500))
    out["search exclude o2"] = _hits(graph.search_entities("*", namespace, exclude_ontology_id=O2, limit=500))
    out["count exclude o2"] = graph.count_entities("*", namespace, exclude_ontology_id=O2)
    out["search page 1"] = _hits(graph.search_entities("*", namespace, kind="class", limit=4, offset=0))
    out["search page 2"] = _hits(graph.search_entities("*", namespace, kind="class", limit=4, offset=4))
    out["count none"] = graph.count_entities("zzz-no-such-thing", namespace)
    out["search label fr"] = _hits(graph.search_entities("compte", namespace, label_preference=fr))
    out["search fr pref"] = _hits(graph.search_entities("*", namespace, kind="class", limit=500, label_preference=fr))
    out["vertex party"] = _sorted_edges(graph.get_vertex(P + "Party", namespace))
    out["vertex party fr"] = _sorted_edges(graph.get_vertex(P + "Account", namespace, label_preference=fr))
    out["vertex loan"] = _sorted_edges(graph.get_vertex(P + "Loan", namespace))
    out["vertex thing"] = _sorted_edges(graph.get_vertex(P + "Thing", namespace))
    out["vertex missing"] = graph.get_vertex(P + "Nope", namespace)
    out["overview o1"] = _overview(graph.get_ontology_overview(O1, namespace))
    out["overview o1 fr"] = _overview(graph.get_ontology_overview(O1, namespace, label_preference=fr))
    out["overview o2"] = _overview(graph.get_ontology_overview(O2, namespace))
    out["overview sample"] = _overview(graph.get_ontology_overview(O1, namespace, sample=True))
    out["overview empty"] = _overview(graph.get_ontology_overview("http://ex.org/none", namespace))
    out["schema"] = graph.get_namespace_schema(namespace, max_results=100)
    out["schema fr"] = graph.get_namespace_schema(namespace, max_results=100, label_preference=fr)
    out["schema no props"] = graph.get_namespace_schema(namespace, max_results=100, include_properties=False)
    out["schema limited"] = graph.get_namespace_schema(namespace, max_results=3)
    out["schema prefix"] = graph.get_namespace_schema(namespace, class_prefix="http://ex.org/ins")
    out["schema prefix hash"] = graph.get_namespace_schema(namespace, class_prefix=P)
    out["mapped backends"] = graph.mapped_class_backends([P + "Account", P + "Loan", P + "Party", "bad uri"], namespace)
    out["mapped sources"] = graph.mapped_class_graph_sources([P + "Account", P + "Loan"], namespace)
    return norm(out)


def turtle_graph(turtle: str | None) -> rdflib.Graph | None:
    """Parse exported Turtle, without the creation timestamps (``None`` stays ``None``)."""
    if turtle is None:
        return None
    g = rdflib.Graph()
    g.parse(data=turtle, format="turtle")
    for stamp in ("created", "modified"):
        g.remove((None, rdflib.URIRef("http://purl.org/dc/terms/" + stamp), None))
    return g


# ── vectors ─────────────────────────────────────────────────────────────

DIMS = 8


def unit(*xs: float) -> list[float]:
    """Unit vector padded to DIMS."""
    v = list(xs) + [0.0] * (DIMS - len(xs))
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def vector_items() -> list[dict]:
    """Embeddings for the scenario."""
    base = {"embedding_type": "text", "model_id": "m1"}
    return [
        {
            **base,
            "entity_uri": P + "Party",
            "ontology_id": O1,
            "entity_type": "class",
            "vector": unit(1, 0.1),
            "text": "party",
        },
        {
            **base,
            "entity_uri": P + "Person",
            "ontology_id": O1,
            "entity_type": "class",
            "vector": unit(1, 0.4),
            "text": "person",
        },
        {
            **base,
            "entity_uri": P + "Account",
            "ontology_id": O1,
            "entity_type": "class",
            "vector": unit(0, 1),
            "text": "account",
        },
        {
            **base,
            "entity_uri": P + "name",
            "ontology_id": O1,
            "entity_type": "property",
            "vector": unit(0.9, 0.3),
            "text": "name",
        },
        {
            **base,
            "entity_uri": P + "Party",
            "ontology_id": O2,
            "entity_type": "class",
            "vector": unit(0.2, 1, 0.5),
            "text": "insured",
        },
        {
            **base,
            "entity_uri": P + "Other",
            "ontology_id": O1,
            "entity_type": "class",
            "vector": unit(1, 0),
            "text": "o",
            "model_id": "m2",
        },
    ]


def vector_results(vec: Any, namespace: str) -> dict[str, Any]:
    """Embedding operations; scores are compared separately because OpenSearch scores are (1 + cos) / 2."""
    out: dict[str, Any] = {}
    vec.store_embedding(vector_items()[0])
    vec.store_embeddings_batch(vector_items()[1:])
    q = unit(1, 0.2)
    hits = vec.search_nearest(q, "text", "m1", top_k=10)
    out["nearest order"] = [(h.entity_uri, h.ontology_id, h.entity_type) for h in hits]
    out["nearest scores"] = [h.score for h in hits]
    hits = vec.search_nearest(q, "text", "m1", entity_type="class", top_k=2)
    out["nearest class top2"] = [(h.entity_uri, h.ontology_id) for h in hits]
    out["nearest o2"] = [h.entity_uri for h in vec.search_nearest(q, "text", "m1", ontology_id=O2, top_k=5)]
    out["nearest other model"] = [h.entity_uri for h in vec.search_nearest(q, "text", "m2", top_k=5)]
    out["nearest other ns"] = vec.search_nearest(q, "text", "m1", namespace="empty-ns")
    out["get entity"] = sorted(
        (d["ontology_id"], d["entity_type"], d["text"]) for d in vec.get_embeddings_for_entity(P + "Party")
    )
    out["get entity class"] = len(
        vec.get_embeddings_for_entity(P + "Party", entity_type="class", embedding_type="text")
    )
    out["list o1"] = sorted(d["entity_uri"] for d in vec.list_embeddings_for_ontology(O1))
    out["list o1 props"] = sorted(d["entity_uri"] for d in vec.list_embeddings_for_ontology(O1, entity_type="property"))
    out["list o1 limit"] = len(vec.list_embeddings_for_ontology(O1, limit=2))
    out["searchable"] = sorted(vec.list_searchable_entity_uris(O1))
    out["delete entities"] = vec.delete_embeddings_for_entities([P + "Party"], O1)
    out["after delete entities"] = sorted(
        (d["ontology_id"], d["entity_uri"])
        for d in vec.list_embeddings_for_ontology(O1) + vec.list_embeddings_for_ontology(O2)
    )
    out["delete ontology"] = vec.delete_embeddings_for_ontology(O1)
    out["delete missing ontology"] = vec.delete_embeddings_for_ontology("http://ex.org/none")
    out["delete index"] = vec.delete_index()
    out["delete index again"] = vec.delete_index()
    out["after delete index"] = vec.list_embeddings_for_ontology(O2)
    return out
