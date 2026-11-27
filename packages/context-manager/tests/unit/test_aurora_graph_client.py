# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""GRAPH_BACKEND=aurora: the Aurora SPARQL graph client and the real queries serve generates.

The queries come from the production builders (T-Box context, graph traverser, FK graph tool,
metric resolver) and run against an rdflib dataset loaded from an in-memory ``coa_triple``.
No test here may reach the network: ``httpx`` is made to fail.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
import pytest
from coa_common.aurora_sparql import AuroraSparql
from coa_common.testing_aurora_graph import InMemoryTripleClient, make_sparql
from coa_serve.clients import factory as client_factory
from coa_serve.clients.aurora_graph import AuroraSparqlGraphClient
from coa_serve.clients.base import GraphClient
from coa_serve.clients.neptune import NeptuneGraphClient, SPARQLValidationError
from coa_serve.clients.sparql_clients import build_sparql_client
from coa_serve.tier1.metric_resolver import MetricResolver, _metric_list_sparql
from coa_serve.tier2.ontop.tbox_context import _IS_MAPPED_IRI, TBoxContextBuilder
from coa_serve.tier2.ontop.types import VectorHit
from coa_serve.tier2.tools.graph_tools import OntologyGraphTool
from coa_serve.tier3.graph_traverser import GraphTraverser

TEMPLATE = "https://ontology-workbench.local/{namespace}"
NS = "demo"
GRAPH = f"https://ontology-workbench.local/{NS}/onto"
BASE = "https://demo.coa/ontology#"
MAPPED = _IS_MAPPED_IRI

ONTOLOGY_TTL = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{BASE}> a owl:Ontology .
<{BASE}Orders> a owl:Class ; <{MAPPED}> true ; rdfs:label "orders" ; rdfs:comment "Customer orders" .
<{BASE}Customers> a owl:Class ; <{MAPPED}> true ; rdfs:label "customers" ; rdfs:comment "Customers" .
<{BASE}Products> a owl:Class ; <{MAPPED}> true ; rdfs:label "products" .
<{BASE}Orders_amount> a owl:DatatypeProperty ; rdfs:label "amount" ;
    rdfs:domain <{BASE}Orders> ; rdfs:range xsd:decimal .
<{BASE}Customers_name> a owl:DatatypeProperty ; rdfs:label "name" ;
    rdfs:domain <{BASE}Customers> ; rdfs:range xsd:string .
<{BASE}Orders_customerid> a owl:ObjectProperty ; rdfs:label "customerid" ;
    rdfs:domain <{BASE}Orders> ; rdfs:range <{BASE}Customers> ;
    rdfs:comment "Foreign key: orders.customerid references customers.id" .
<{BASE}Orders_productid> a owl:ObjectProperty ; rdfs:label "productid" ;
    rdfs:domain <{BASE}Orders> ; rdfs:range <{BASE}Products> ;
    rdfs:comment "Foreign key: orders.productid references products.id" .
"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a Neptune HTTP call was made")

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)
    monkeypatch.setattr(httpx.Client, "post", refuse)


@pytest.fixture
def store() -> tuple[AuroraSparql, InMemoryTripleClient]:
    sparql, client = make_sparql(ttl_s=60)
    client.load_turtle(ONTOLOGY_TTL, GRAPH, NS)
    return sparql, client


@pytest.fixture
def graph(store) -> AuroraSparqlGraphClient:
    return AuroraSparqlGraphClient(store[0])


# ── the client ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestAuroraSparqlGraphClient:
    async def test_is_a_graph_client(self, graph):
        assert isinstance(graph, GraphClient)

    async def test_query_ask_construct_have_neptune_client_shapes(self, graph):
        rows = await graph.query(
            f"SELECT ?c ?l WHERE {{ GRAPH <{GRAPH}> {{ ?c a <http://www.w3.org/2002/07/owl#Class> ; "
            f"<http://www.w3.org/2000/01/rdf-schema#label> ?l }} }} ORDER BY ?l"
        )
        assert rows == [
            {"c": f"{BASE}Customers", "l": "customers"},
            {"c": f"{BASE}Orders", "l": "orders"},
            {"c": f"{BASE}Products", "l": "products"},
        ]
        assert await graph.ask(f"ASK {{ GRAPH <{GRAPH}> {{ <{BASE}Orders> ?p ?o }} }}") is True
        triples = await graph.construct(
            f"CONSTRUCT {{ <{BASE}Orders> ?p ?o }} WHERE {{ GRAPH <{GRAPH}> {{ <{BASE}Orders> ?p ?o }} }}"
        )
        assert {
            "subject": f"{BASE}Orders",
            "predicate": "http://www.w3.org/2000/01/rdf-schema#label",
            "object": "orders",
        } in triples

    async def test_write_queries_are_rejected_but_keywords_in_data_are_not(self, graph):
        with pytest.raises(SPARQLValidationError):
            await graph.query(f"DELETE WHERE {{ GRAPH <{GRAPH}> {{ ?s ?p ?o }} }}")
        rows = await graph.query(
            f'SELECT ?s WHERE {{ GRAPH <{GRAPH}> {{ ?s ?p ?o }} FILTER(CONTAINS(STR(?o), "clear drop")) }}'
        )
        assert rows == []

    async def test_health_check_never_raises(self, graph):
        assert await graph.health_check() == {"status": "ok"}

        class Broken:
            def health_check(self):
                raise RuntimeError("down")

        assert (await AuroraSparqlGraphClient(Broken()).health_check())["status"] == "error"  # type: ignore[arg-type]

    async def test_close_is_a_no_op(self, graph):
        await graph.close()
        assert await graph.ask(f"ASK {{ GRAPH <{GRAPH}> {{ ?s ?p ?o }} }}") is True


# ── factory and source client selection ─────────────────────────────


@pytest.mark.unit
class TestSelection:
    def test_default_builds_neptune(self, monkeypatch):
        monkeypatch.delenv("GRAPH_BACKEND", raising=False)
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "neptune.example.internal")
        monkeypatch.setattr(client_factory, "build_vector_client", lambda *a, **k: object())
        monkeypatch.setattr("coa_serve.clients.bedrock.BedrockLLMClient", lambda *a, **k: object())
        monkeypatch.setattr("coa_serve.clients.source_db.SourceDBQueryExecutor", lambda *a, **k: object())
        clients = client_factory._build_default_clients()
        assert isinstance(clients.graph, NeptuneGraphClient)

    def test_aurora_builds_the_aurora_client_without_a_neptune_endpoint(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
        monkeypatch.setattr(client_factory, "build_vector_client", lambda *a, **k: object())
        monkeypatch.setattr("coa_serve.clients.bedrock.BedrockLLMClient", lambda *a, **k: object())
        monkeypatch.setattr("coa_serve.clients.source_db.SourceDBQueryExecutor", lambda *a, **k: object())
        clients = client_factory._build_default_clients()
        assert isinstance(clients.graph, AuroraSparqlGraphClient)

    def test_source_data_endpoints_stay_on_their_own_neptune_cluster(self, monkeypatch):
        # A graph source's data endpoint is the customer's own Neptune cluster (the A-Box), never the
        # deployment's Aurora ontology tables, so GRAPH_BACKEND does not redirect it.
        from coa_common.sparql_config import SparqlSourceConfig

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        source = SparqlSourceConfig(dataEndpoint="db.cluster-abc123.us-east-1.neptune.amazonaws.com")
        assert isinstance(build_sparql_client(source, default_region="us-east-1"), NeptuneGraphClient)


# ── the production queries, against the adapter ─────────────────────


def _class_hit(uri: str) -> VectorHit:
    return VectorHit(
        type="ontology_class",
        score=0.9,
        entity_id=uri,
        uri=uri,
        metadata={"type": "ontology_class", "entity_id": uri, "uri": uri},
    )


@pytest.mark.unit
class TestProductionQueries:
    async def test_tier2_tbox_context(self, graph):
        builder = TBoxContextBuilder(graph, graph_uri_template=TEMPLATE)
        context = await builder.build([_class_hit(f"{BASE}Orders"), _class_hit(f"{BASE}Customers")], NS)
        uris = {c["uri"] for c in context.classes}
        assert {f"{BASE}Orders", f"{BASE}Customers"} <= uris
        assert any(p.get("label") == "amount" or "amount" in json.dumps(p) for p in context.properties)

    async def test_tier2_tbox_entity_fallback(self, graph):
        builder = TBoxContextBuilder(graph, graph_uri_template=TEMPLATE)
        context = await builder.build([], NS, query="show me customers")
        assert any(c["uri"] == f"{BASE}Customers" for c in context.classes)

    async def test_tier3_graph_traverser_keyword_uri_and_bfs(self, graph):
        traverser = GraphTraverser(graph, graph_uri_template=TEMPLATE)
        by_keyword = await traverser.traverse("which orders exist", NS, entities=["orders"])
        assert any(e.uri == f"{BASE}Orders" for e in by_keyword)
        from_uri = await traverser.traverse_from_uris([f"{BASE}Orders"], NS, max_hops=1)
        assert from_uri
        walked = await traverser.traverse_graph(f"{BASE}Orders", NS, max_hops=1)
        assert isinstance(walked, list)

    async def test_tier2_graph_tools_fk_walk(self, graph):
        tool = OntologyGraphTool(graph, namespace=NS, graph_uri_template=TEMPLATE)
        result = await tool.explore_graph("orders", hops=1)
        names = {n["table"] for n in result["tables"]}
        assert {"customers", "products"} <= names, result
        reached = await tool.expand_from([f"{BASE}Orders"], hops=1)
        assert {r["table"] for r in reached} == {"customers", "products"}

    async def test_tier1_metric_resolver_loads_governed_metrics(self, store):
        sparql, client = store
        from coa_common import GOVERNED_METRICS_ONTOLOGY_ID
        from coa_common.constants import URN_PREFIX

        vocab = f"urn:{URN_PREFIX}:vocab#"
        graph_iri = f"https://ontology-workbench.local/{NS}/{quote(GOVERNED_METRICS_ONTOLOGY_ID, safe='')}"
        m = f"urn:{URN_PREFIX}:{NS}:metric:revenue"
        dialects = json.dumps(json.dumps([{"dialect": "sql", "expression": "SUM(amount)"}]))
        client.load_turtle(
            f"""
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            <{m}> a owl:Class ; rdfs:subClassOf <{vocab}GovernedMetric> ; rdfs:label "revenue" ;
                rdfs:comment "Total revenue" ;
                <{vocab}dataSourceId> "ds-1" ;
                <{vocab}expressionDialects> {dialects} .
            """,
            graph_iri,
            NS,
        )
        resolver = MetricResolver(neptune_client=AuroraSparqlGraphClient(sparql))
        await resolver._refresh_from_neptune()
        found = await resolver.definitions_named("revenue", NS)
        assert found and found[0].name == "revenue"
        # the namespace-scoped (constant GRAPH) form of the same query
        rows = await AuroraSparqlGraphClient(sparql).query(_metric_list_sparql(TEMPLATE, namespace=NS))
        assert [r["name"] for r in rows] == ["revenue"]
        assert rows[0]["g"] == graph_iri


# ── no Neptune endpoint: GraphRAG / unstructured features degrade cleanly ──


@pytest.mark.unit
class TestWithoutNeptuneEndpoint:
    def _config(self):
        from coa_serve.config import ServiceConfig

        return ServiceConfig(
            guardrail_id="",
            vkg_endpoint="http://vkg.local:8080",
            neptune_endpoint="",
            opensearch_endpoint="aoss.example.internal",
            bedrock_model_id="model",
            bedrock_region="us-east-1",
            data_sources_table="",
            metric_definitions_table="",
            memory_id="",
            session_metadata_table="",
        )

    def test_lexical_retriever_is_not_built(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        assert client_factory.build_lexical_retriever(self._config()) is None

    def test_graph_tools_are_skipped_and_the_ontology_tool_stays(self, monkeypatch, graph):
        from types import SimpleNamespace

        import structlog
        from coa_serve.tier3.agentic.factory import build_tool_registry

        monkeypatch.setenv("GRAPH_BACKEND", "aurora")
        clients = SimpleNamespace(graph=graph, vector=object(), llm=None)
        with structlog.testing.capture_logs() as logs:
            registry = build_tool_registry(self._config(), clients)
        names = registry.names()
        assert "ontology_lookup" in names
        assert "graph_traversal" not in names
        assert "chunk_window" not in names
        assert not any(n.startswith("strategy:") for n in names)
        omitted = {e["tool"] for e in logs if e.get("event") == "agentic_tool_omitted"}
        assert "graph_traversal" in omitted
