# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Drive the pinned graphrag-lexical-graph toolkit against a recording store.

``collect_queries`` runs every query-emitting path COA enables (the builders, each retrieval
strategy in ``coa_serve.lexical.strategies``, ``VersionManager``, ``DeleteSources``, ``get_sources``
and the source-id guards) against a store that records each Cypher text and answers with canned
rows. No AWS call is made. The golden test compares the canonical fingerprints it returns to the
checked-in ``graphrag_pg_fingerprints.json``.
"""

from __future__ import annotations

# ruff: noqa: E501
import contextlib
import inspect
import re
from typing import Any

from coa_common.graphrag_pg.store import AuroraGraphStore, canonicalize

TENANT = "abc"

# Rows a toolkit flow needs to get past its first query, keyed by the start of the canonical Cypher.
# ``once`` entries answer the first call only, so the toolkit's "repeat until empty" loops end.
_CYPHER_ROWS: dict[str, list[Any]] = {
    "MATCH (n:`__Source__`) RETURN count(n)": [{"count": 1}],
    "// get top entities for chunk (semantic beam search)": [{"entityId": "e1", "score": 3}],
    "// get complements matching subject": [{"n_id": "e3", "c_id": "e9"}],
    "// get subjects matching complement": [{"n_id": "e3", "c_id": "e9"}],
    "// Get statements for top chunk": [{"result": {"statement": "Acme owes Beta", "statementId": "st1"}}],
    "// get next level in tree": [
        {"result": {"entity": {"entityId": "e1", "value": "Acme", "class": "Company"}, "others": ["e2"]}}
    ],
}
_CYPHER_ONCE: dict[str, list[Any]] = {
    "// get statement ids (delete source)": [{"statementId": "st1"}],
    "// get topic ids (delete source)": [{"topicId": "t1"}],
    "// get chunk ids (delete source)": [{"chunkId": "c1"}],
    "// get fact ids (delete source)": [{"factId": "f1"}],
    "// get entity ids (delete source)": [{"entityId": "e1"}],
    "// get orphaned fact ids (delete source)": [{"factId": "f1"}],
    "// get orphaned entity ids (delete source)": [{"entityId": "e1"}],
}
# Same idea one level down: rows ``_q`` returns, keyed by statement kind.
_SQL_ROWS: dict[str, list[Any]] = {
    "read:chunk_top_entities": [{"entityId": "e1", "score": "3"}],
    "read:local_subject_complements": [{"n_id": "e3", "c_id": "e9"}],
    "read:local_complement_subjects": [{"n_id": "e3", "c_id": "e9"}],
    "read:top_statements": [{"statementId": "st1", "statement": "Acme owes Beta"}],
    "read:tree_level": [{"entityId": "e1", "value": "Acme", "cls": "Company", "others": '["e2"]'}],
}


class RecordingStore(AuroraGraphStore):
    """Store for driving the toolkit offline.

    ``sql_mode=False`` stops at the Cypher boundary: each canonical query is recorded and answered from
    canned rows. ``sql_mode=True`` runs the real handler dispatch and records each ``(kind, sql, params)``
    the handlers send to Aurora, answering with canned rows.
    """

    def __init__(self, sql_mode: bool = False) -> None:
        super().__init__()
        object.__setattr__(self, "_sql_mode", sql_mode)
        object.__setattr__(self, "_flats", [])
        object.__setattr__(self, "_calls", [])  # (cypher, parameters)
        object.__setattr__(self, "_sql", [])  # (kind, sql, params)
        object.__setattr__(self, "_answered", set())

    def _once(self, key: str, rows: list[Any]) -> list[Any]:
        if key in self._answered:
            return []
        self._answered.add(key)
        return list(rows)

    def execute_query_with_retry(self, query, parameters, max_attempts=3, max_wait=5, **kwargs):  # type: ignore[override]
        result = super().execute_query_with_retry(query, parameters, max_attempts, max_wait, **kwargs)
        if inspect.isgenerator(result):  # a QueryTree is lazy; the batch client drains it
            result = list(result)
        return result

    def _execute_query(self, cypher, parameters=None, correlation_id=None):  # type: ignore[override]
        canon, _tenant = canonicalize(cypher)
        flat = " ".join(canon.split())
        self._flats.append(flat)
        self._calls.append((cypher, parameters or {}))
        if self._sql_mode:
            return super()._execute_query(cypher, parameters, correlation_id)
        if "RETURN count(n) AS count" in flat:
            return [{"count": 1}]
        for prefix, rows in _CYPHER_ROWS.items():
            if flat.startswith(prefix):
                return list(rows)
        for prefix, rows in _CYPHER_ONCE.items():
            if flat.startswith(prefix):
                return self._once(flat, rows)
        return []

    def _q(self, kind, sql, params=None):  # type: ignore[override]
        self._sql.append((kind, sql, params or {}))
        if kind == "read:delete_ids":
            alias = re.search(r'AS "(\w+)"', sql)
            return self._once(sql, [{alias.group(1): "x1"}]) if alias else []
        return list(_SQL_ROWS.get(kind, []))


class _Vec:
    """Vector index stand-in: ``top_k`` returns one element per index kind."""

    index_name = "chunk"

    def __init__(self, name: str) -> None:
        self.index_name = name

    def top_k(self, query_bundle: Any, top_k: int = 5, filter_config: Any = None) -> list[dict[str, Any]]:
        key = f"{self.index_name}Id"
        return [{self.index_name: {key: f"{self.index_name}:1"}, "score": 0.9}]

    def delete_embeddings(self, ids: list[str]) -> None:
        return None

    def underlying_index_name(self) -> str:
        return self.index_name


class _VecStore:
    def __init__(self, dummy_topic: bool = False) -> None:
        self.dummy_topic = dummy_topic

    def get_index(self, name: str) -> Any:
        if name == "topic" and self.dummy_topic:
            from graphrag_toolkit.lexical_graph.storage.vector.dummy_vector_index import DummyVectorIndex

            return DummyVectorIndex(index_name="topic")
        return _Vec(name)

    def all_indexes(self) -> list[Any]:
        return [_Vec("chunk"), _Vec("topic"), _Vec("statement")]


def _tenant_store(sql_mode: bool) -> tuple[RecordingStore, Any]:
    from graphrag_toolkit.lexical_graph.storage.graph.multi_tenant_graph_store import MultiTenantGraphStore
    from graphrag_toolkit.lexical_graph.tenant_id import to_tenant_id

    inner = RecordingStore(sql_mode)
    return inner, MultiTenantGraphStore.wrap(inner, to_tenant_id(TENANT))


def _query_bundle(text: str = "who owes whom"):
    from llama_index.core.schema import QueryBundle

    return QueryBundle(query_str=text, embedding=[0.1, 0.2, 0.3])


def _retrievers(graph: Any) -> None:
    """One call per query-emitting method of every retriever COA's strategies construct."""
    from graphrag_toolkit.lexical_graph.retrieval.processors import ProcessorArgs
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import (
        ChunkBasedSearch,
        ChunkBasedSemanticSearch,
        EntityBasedSearch,
        EntityContextSearch,
        EntityNetworkSearch,
        TopicBasedSearch,
        TopicBeamSearch,
    )
    from graphrag_toolkit.lexical_graph.retrieval.retrievers.semantic_chunk_beam_search import (
        SemanticChunkBeamGraphSearch,
    )

    args = ProcessorArgs(include_chunk_details=True, intermediate_limit=25)
    vs = _VecStore()
    ChunkBasedSearch(graph, vs, processor_args=args).chunk_based_graph_search("chunk:1")
    ChunkBasedSemanticSearch(graph, vs, processor_args=args).chunk_based_graph_search("chunk:1")
    ebs = EntityBasedSearch(graph, vs, processor_args=args)
    ebs._multiple_entity_based_graph_search("e1", ["e2", "e3"], _query_bundle())
    ebs._single_entity_based_graph_search("e1", _query_bundle())
    EntityNetworkSearch(graph, vs, processor_args=args)._graph_search("topic:1")
    EntityNetworkSearch(graph, _VecStore(dummy_topic=True), processor_args=args)._graph_search("chunk:1")
    TopicBasedSearch(graph, vs, processor_args=args).topic_based_graph_search("topic:1")
    EntityContextSearch(graph, vs, processor_args=args)  # runs no query itself: sub-retrievers do
    beam = SemanticChunkBeamGraphSearch.__new__(SemanticChunkBeamGraphSearch)
    beam.graph_store = graph
    beam.beam_width = 10
    beam.get_neighbors("chunk:1")
    for include_chunk_details in (True, False):
        r = ChunkBasedSearch(
            graph, vs, processor_args=ProcessorArgs(include_chunk_details=include_chunk_details, intermediate_limit=25)
        )
        r.get_statements_by_topic_and_source(["st1", "st2"])
    for entity, same, adjacent in (
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ):
        tb = TopicBeamSearch(
            graph_store=graph,
            vector_store=vs,
            processor_args=args,
            use_entity_neighbors=entity,
            use_same_chunk_neighbors=same,
            use_adjacent_chunk_neighbors=adjacent,
        )
        tb.get_neighbors_batch(["topic:1"])
    tb._expand_topics_to_statement_ids(["topic:1"])


def _providers(graph: Any) -> None:
    from graphrag_toolkit.lexical_graph.retrieval.processors import ProcessorArgs
    from graphrag_toolkit.lexical_graph.retrieval.query_context.entity_context_provider import EntityContextProvider
    from graphrag_toolkit.lexical_graph.retrieval.query_context.entity_from_top_statement_provider import (
        EntityFromTopStatementProvider,
    )
    from graphrag_toolkit.lexical_graph.retrieval.query_context.entity_provider import EntityProvider
    from graphrag_toolkit.lexical_graph.retrieval.query_context.entity_vss_provider import EntityVSSProvider
    from graphrag_toolkit.lexical_graph.retrieval.query_context.keyword_vss_provider import KeywordVSSProvider

    args = ProcessorArgs(intermediate_limit=25)
    ep = EntityProvider(graph, args)
    ep._get_entities_for_keyword("acme corp")
    ep._get_entities_for_keyword("acme corp|Company")
    for index in ("topic", "chunk"):
        vss = EntityVSSProvider.__new__(EntityVSSProvider)
        vss.graph_store, vss.args, vss.index_name = graph, args, index
        vss._get_entities_for_nodes([f"{index}:1"])
        top = EntityFromTopStatementProvider.__new__(EntityFromTopStatementProvider)
        top.graph_store, top.args, top.index_name = graph, args, index
        top.vector_store = _VecStore()
        top._get_top_statement_id(_query_bundle())
    top._get_entities_for_statement("st1")
    kw = KeywordVSSProvider.__new__(KeywordVSSProvider)
    kw.graph_store, kw.args = graph, args
    kw._get_topic_content(["topic:1"])
    ecp = EntityContextProvider(graph, ProcessorArgs(ec_max_depth=3))
    from graphrag_toolkit.lexical_graph.retrieval.model import Entity, ScoredEntity

    scored = ScoredEntity(entity=Entity(entityId="e1", value="Acme", classification="Company"), score=2.0)
    tree = ecp._get_entity_id_context_tree([scored])
    ecp._get_neighbour_entities(tree)


def _utils(graph: Any) -> None:
    from graphrag_toolkit.lexical_graph.retrieval.utils import vector_utils
    from graphrag_toolkit.lexical_graph.retrieval.utils.chunk_utils import get_chunks_query
    from graphrag_toolkit.lexical_graph.retrieval.utils.statement_utils import get_statements_query
    from graphrag_toolkit.lexical_graph.storage.chunk.in_graph_chunk_store import InGraphChunkStore

    get_chunks_query(graph, ["c1"])
    get_statements_query(graph, ["st1"])
    vector_utils._cache.clear() if hasattr(vector_utils, "_cache") else None
    vector_utils._resolve_source_ids(graph, "topic", [{"topic": {"topicId": "topic:zz-unique"}}])
    store = InGraphChunkStore(graph_client=graph)
    store.get_batch(["c1"])
    store.put_batch({"c1": "chunk text"})


def _builders(graph: Any, plain: Any) -> None:
    from graphrag_toolkit.lexical_graph.indexing.build.chunk_graph_builder import ChunkGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.entity_graph_builder import EntityGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.entity_relation_graph_builder import EntityRelationGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.fact_graph_builder import FactGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.graph_summary_builder import GraphSummaryBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.local_entity_rewrites_graph_builder import (
        LocalEntityRewritesGraphBuilder,
    )
    from graphrag_toolkit.lexical_graph.indexing.build.source_graph_builder import SourceGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.statement_graph_builder import StatementGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.build.topic_graph_builder import TopicGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.model import Entity, Fact, Relation, Statement, Topic
    from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode

    sources = [
        {"sourceId": "src:1", "metadata": {"filename": "a.docx"}},
        {
            "sourceId": "src:2",
            "metadata": {"filename": "b.docx"},
            "sourceHash": "h",
            "versioning": {
                "valid_from": 1,
                "valid_to": 9,
                "extract_timestamp": 1,
                "build_timestamp": 2,
                "id_fields": ["k"],
                "prev_versions": ["src:1"],
            },
        },
    ]
    for s in sources:
        SourceGraphBuilder().build(TextNode(text="", metadata={"source": s}), graph)
    chunk = TextNode(
        text="Acme owes Beta.",
        metadata={"chunk": {"chunkId": "chunk:1", "metadata": {"page": 3}}},
        relationships={
            NodeRelationship.SOURCE: RelatedNodeInfo(node_id="src:1"),
            NodeRelationship.NEXT: RelatedNodeInfo(node_id="chunk:2"),
            NodeRelationship.PREVIOUS: RelatedNodeInfo(node_id="chunk:0"),
            NodeRelationship.PARENT: RelatedNodeInfo(node_id="chunk:p"),
            NodeRelationship.CHILD: RelatedNodeInfo(node_id="chunk:c"),
        },
    )
    ChunkGraphBuilder().build(chunk, graph)
    topic = Topic(topicId="topic:1", value="Debt", chunkIds=["chunk:1"])
    TopicGraphBuilder().build(TextNode(text="", metadata={"topic": topic.model_dump()}), graph)
    st1 = Statement(statementId="st1", topicId="topic:1", chunkId="chunk:1", value="Acme owes Beta", details=["d1"])
    st0 = Statement(statementId="st0", topicId="topic:1", chunkId="chunk:1", value="Beta lends", details=[])
    prev = RelatedNodeInfo(node_id="st0", metadata={"statement": st0.model_dump()})
    StatementGraphBuilder().build(
        TextNode(text="", metadata={"statement": st1.model_dump()}, relationships={NodeRelationship.PREVIOUS: prev}),
        graph,
    )
    acme = Entity(entityId="e1", value="Acme Corp", classification="Company")
    beta = Entity(entityId="e2", value="Beta", classification="Lender")
    local = Entity(entityId="e3", value="the Borrower", classification="__Local_Entity__")
    facts = [
        Fact(factId="f1", statementId="st1", subject=acme, predicate=Relation(value="owes"), object=beta),
        Fact(
            factId="f2",
            statementId="st1",
            subject=acme,
            predicate=Relation(value="is"),
            complement=Entity(entityId="e9", value="a borrower", classification="__Local_Entity__"),
        ),
        Fact(factId="f3", statementId="st1", subject=local, predicate=Relation(value="owes"), object=beta),
    ]
    for include_local in (False, True):
        for fact in facts:
            node = TextNode(text="Acme owes Beta", metadata={"fact": fact.model_dump()})
            for builder in (FactGraphBuilder, EntityGraphBuilder, EntityRelationGraphBuilder, GraphSummaryBuilder):
                builder().build(node, graph, include_domain_labels=False, include_local_entities=include_local)
            # The toolkit's MultiTenantGraphStore cannot rewrite a QueryTree, so the local-entity
            # rewrites only run for the default tenant (an un-wrapped store).
            LocalEntityRewritesGraphBuilder().build(
                node, plain, include_domain_labels=False, include_local_entities=include_local
            )


def _versioning_and_guards(graph: Any) -> None:
    from graphrag_toolkit.lexical_graph import LexicalGraphIndex
    from graphrag_toolkit.lexical_graph.indexing.build.delete_sources import DeleteSources
    from graphrag_toolkit.lexical_graph.indexing.build.version_manager import VersionManager
    from graphrag_toolkit.lexical_graph.indexing.source_id_collision import SourceIdClaims
    from graphrag_toolkit.lexical_graph.indexing.source_id_width import (
        SourceIdWidth,
        record_graph_source_id_width,
        recorded_source_id_width,
        sampled_source_id_width,
    )
    from graphrag_toolkit.lexical_graph.tenant_id import to_tenant_id
    from llama_index.core.schema import TextNode

    vs = _VecStore()
    vm = VersionManager.model_construct(graph_store=graph, vector_store=vs)
    node = TextNode(text="", metadata={"source": {"sourceId": "src:3", "metadata": {"k": "v", "n": "1"}}})
    vm._get_existing_source_nodes(["k"], node)
    for name in ("chunk", "topic", "statement"):
        vm._get_node_ids(_Vec(name), ["src:1"])
    vm._set_source_node_version_info("src:1", 5, ["k"])

    ds = DeleteSources.model_construct(graph_store=graph, vector_store=vs, num_workers=1, batch_size=1000)
    ds.delete_source_document("src:1")
    for name in ("get_statements_ids", "get_topic_ids", "get_chunk_ids"):
        getattr(ds, name)("src:1")
    ds.get_facts_ids(["st1"])
    ds.get_entity_ids(["f1"])
    ds.get_orphaned_fact_ids(["f1"])
    ds.get_orphaned_entity_ids(["e1"])
    for name, arg in (
        ("delete_entities", ["e1"]),
        ("delete_facts", ["f1"]),
        ("delete_statements", ["st1"]),
        ("delete_topics", ["t1"]),
        ("delete_chunks", ["c1"]),
    ):
        getattr(ds, name)(arg)
    ds.delete_source("src:1")

    index = LexicalGraphIndex.__new__(LexicalGraphIndex)
    index.graph_store = graph
    index.get_sources(source_ids=["src:1"])
    index.get_sources(filter={"doc_source_id": "ds1"})
    index.get_sources(source_id="src:1", filter={"doc_source_id": "ds1", "k": 4})

    recorded_source_id_width(graph)
    sampled_source_id_width(graph)
    record_graph_source_id_width(
        graph, to_tenant_id(TENANT), SourceIdWidth.WIDE if hasattr(SourceIdWidth, "WIDE") else list(SourceIdWidth)[0]
    )
    claims = SourceIdClaims()
    claims._verify_batch(graph, ["src:1", "src:2"])


def unsupported_calls() -> list[tuple[str, dict[str, Any]]]:
    """Queries the pinned toolkit can emit that COA never enables. The store must refuse every one.

    ``get_graph_summary`` and ``get_stats`` have no caller in COA; ``include_domain_labels`` and ``order_by``
    are off by default and unused.
    """
    from graphrag_toolkit.lexical_graph import LexicalGraphIndex
    from graphrag_toolkit.lexical_graph.indexing.build.entity_graph_builder import EntityGraphBuilder
    from graphrag_toolkit.lexical_graph.indexing.model import Entity, Fact, Relation
    from graphrag_toolkit.lexical_graph.retrieval.summary.graph_summary import GraphSummary
    from graphrag_toolkit.lexical_graph.tenant_id import to_tenant_id
    from llama_index.core.schema import TextNode

    inner, graph = _tenant_store(sql_mode=False)
    index = LexicalGraphIndex.__new__(LexicalGraphIndex)
    index.graph_store = graph
    index.get_sources(source_id="src:1", order_by="filename")
    with contextlib.suppress(Exception):  # empty canned rows end the flow early; the queries are recorded
        index.get_stats()
    summary = GraphSummary.__new__(GraphSummary)
    summary.graph_store = graph
    tenant = to_tenant_id(TENANT)
    for call in (
        lambda: summary._get_entities(tenant),
        lambda: summary._get_paths(tenant),
        lambda: summary._get_cached_summary(tenant),
        lambda: summary._cache_summary(tenant, "text"),
    ):
        with contextlib.suppress(Exception):
            call()
    fact = Fact(
        factId="f1",
        statementId="st1",
        subject=Entity(entityId="e1", value="Acme", classification="Company"),
        predicate=Relation(value="owes"),
        object=Entity(entityId="e2", value="Beta", classification="Lender"),
    )
    EntityGraphBuilder().build(
        TextNode(text="", metadata={"fact": fact.model_dump()}),
        graph,
        include_domain_labels=True,
        include_local_entities=False,
    )
    return list(inner._calls)


def run(sql_mode: bool = False) -> RecordingStore:
    """Drive every COA-enabled toolkit path; return the store holding what it recorded."""
    inner, graph = _tenant_store(sql_mode)
    _retrievers(graph)
    _providers(graph)
    _utils(graph)
    _builders(graph, inner)
    _versioning_and_guards(graph)
    return inner


def collect_queries() -> list[str]:
    """Return the sorted, de-duplicated canonical query texts the toolkit emits on COA-enabled paths."""
    return sorted(set(run()._flats))


if __name__ == "__main__":
    import json
    import sys

    json.dump(collect_queries(), sys.stdout, indent=1)
