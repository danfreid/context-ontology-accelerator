# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline tests for the Aurora GraphRAG store: Cypher recognition, no AWS calls."""

from __future__ import annotations

import pytest

pytest.importorskip("graphrag_toolkit")

from coa_common.graphrag_pg.store import (  # noqa: E402
    AuroraGraphStore,
    UnsupportedGraphQueryError,
    canonicalize,
    parse_write,
    split_label,
    translate_source_filter,
)

TOPIC = """// insert topics
UNWIND $params AS params
MERGE (topic:`__Topic__t1__`{topicId: params.topic_id})
ON CREATE SET topic.value=params.title
ON MATCH SET topic.value=params.title
WITH topic, params
UNWIND params.chunk_ids as chunkIds
MERGE (chunk:`__Chunk__t1__`{chunkId: chunkIds.chunk_id})
MERGE (topic)-[:`__MENTIONED_IN__`]->(chunk)"""

SOURCE = """// insert source
UNWIND $params AS params
MERGE (source:`__Source__t1__`{sourceId: params.sourceId})
WITH source, params, source.sourceHash AS owner
WHERE owner IS NULL OR owner = params.`__source_hash__`
SET source.`filename` = params.`filename`
SET source.sourceHash = coalesce(owner, params.`__source_hash__`)"""


def test_split_label_and_canonicalize() -> None:
    assert split_label("__Entity__abc__") == ("__Entity__", "abc")
    assert split_label("__Entity__") == ("__Entity__", "")
    assert split_label("__BELONGS_TO__") is None
    canon, tenant = canonicalize("MATCH (e:`__Entity__abc__`)-[:`__SUPPORTS__`]->(s:`__Statement__abc__`)")
    assert canon == "MATCH (e:`__Entity__`)-[:`__SUPPORTS__`]->(s:`__Statement__`)"
    assert tenant == "abc"


def test_topic_write_creates_nested_edges_and_stub_chunks() -> None:
    plan = parse_write(
        TOPIC, [{"topic_id": "t:1", "title": "T", "chunk_ids": [{"chunk_id": "c:1"}, {"chunk_id": "c:2"}]}]
    )
    by_id = {n.id: n for n in plan.nodes}
    assert by_id["t:1"].real and by_id["t:1"].props == {"value": "T"} and by_id["t:1"].tenant == "t1"
    assert not by_id["c:1"].real
    assert sorted(plan.edges) == [("__MENTIONED_IN__", "t:1", "c:1", ""), ("__MENTIONED_IN__", "t:1", "c:2", "")]


def test_source_write_carries_hash_guard() -> None:
    plan = parse_write(SOURCE, [{"sourceId": "s:1", "filename": "a.pdf", "__source_hash__": "h"}])
    assert plan.hash_guard == "sourceHash"
    assert plan.nodes[0].props == {"filename": "a.pdf", "sourceHash": "h"}


def test_unrecognised_write_fails_closed() -> None:
    with pytest.raises(UnsupportedGraphQueryError):
        parse_write("UNWIND $params AS params MATCH (n) DETACH DELETE n", [{"x": 1}])


def test_unknown_read_is_not_retried_and_raises() -> None:
    store = AuroraGraphStore()
    assert UnsupportedGraphQueryError in store.unretriable_exception_types()
    with pytest.raises(UnsupportedGraphQueryError):
        store._execute_query("MATCH (n:`__Chunk__`) RETURN count(n) AS c", {})


def test_filter_translation_binds_every_value() -> None:
    sql, params = translate_source_filter("((source.`k` = 'a''b') AND (source.`n` >= 5))")
    assert "a'b" not in sql and "'b" not in sql
    assert params["fv0"] == "a'b" and params["fk0"] == "k" and params["fv1"] == 5.0


def test_filter_translation_rejects_unsupported_syntax() -> None:
    with pytest.raises(UnsupportedGraphQueryError):
        translate_source_filter("(source.`k` = 'a'); DROP TABLE x")


# ── chunk_based_semantic read shapes: every query the retriever sends must match a handler ──


class _RecordingStore(AuroraGraphStore):
    """Store whose SQL layer returns canned rows by statement kind and records what ran."""

    def __init__(self, rows: dict[str, list[dict]] | None = None) -> None:
        super().__init__()
        object.__setattr__(self, "_canned", rows or {})
        object.__setattr__(self, "_seen", [])

    def _q(self, kind, sql, params=None):  # type: ignore[override]
        self._seen.append((kind, sql, params))
        return list(self._canned.get(kind, []))


def _semantic(store: AuroraGraphStore, limit: int = 7):
    from graphrag_toolkit.lexical_graph.retrieval.retrievers.chunk_based_semantic_search import (
        ChunkBasedSemanticSearch,
    )

    class _Args:
        intermediate_limit = limit

    o = ChunkBasedSemanticSearch.__new__(ChunkBasedSemanticSearch)
    o.graph_store = store
    o.args = _Args()
    return o


def test_chunk_statement_lookup_has_a_handler() -> None:
    store = _RecordingStore({"read:chunk_statements": [{"l": "s1"}, {"l": "s2"}]})
    assert _semantic(store).chunk_based_graph_search("c1") == ["s1", "s2"]
    kind, sql, params = store._seen[0]
    assert kind == "read:chunk_statements"
    assert params == {"cid": "c1"}
    assert "LIMIT 7" in sql
    assert "__BELONGS_TO__" in sql and "__MENTIONED_IN__" in sql


def test_beam_neighbour_queries_have_handlers() -> None:
    from graphrag_toolkit.lexical_graph.retrieval.retrievers.semantic_chunk_beam_search import (
        SemanticChunkBeamGraphSearch,
    )

    store = _RecordingStore(
        {
            "read:chunk_top_entities": [{"entityId": "e1", "score": "3"}],
            "read:chunk_neighbours": [{"chunkId": "c2", "shared": "2"}],
        }
    )
    beam = SemanticChunkBeamGraphSearch.__new__(SemanticChunkBeamGraphSearch)
    beam.graph_store = store
    beam.beam_width = 3
    assert beam.get_neighbors("c1") == ["c2"]
    assert [k for k, _, _ in store._seen] == ["read:chunk_top_entities", "read:chunk_neighbours"]
    assert store._seen[1][2]["ids"] == '["e1"]'
    assert "LIMIT 6" in store._seen[1][1]


def test_chunk_neighbours_with_no_entities_runs_no_sql() -> None:
    store = _RecordingStore()
    cypher = (
        "// get neighboring chunks for common entities (semantic beam search) MATCH (entity) "
        "WHERE entity.entityId IN $entityIds RETURN 1"
    )
    assert store._execute_query(cypher, {"entityIds": [], "chunkId": "c1", "limit": 6}) == []
    assert store._seen == []


def test_chunks_with_source_returns_property_maps() -> None:
    from graphrag_toolkit.lexical_graph.retrieval.utils.chunk_utils import get_chunks_query

    store = _RecordingStore(
        {
            "read:chunks_with_source": [
                {
                    "cid": "c1",
                    "cvalue": "chunk text",
                    "csearch": None,
                    "cclass": None,
                    "cprops": '{"page": 3}',
                    "sid": "src1",
                    "sprops": '{"filename": "a.docx"}',
                }
            ]
        }
    )
    out = get_chunks_query(store, ["c1", "missing"])
    assert out == [
        {
            "result": {
                "source": {"sourceId": "src1", "metadata": {"sourceId": "src1", "filename": "a.docx"}},
                "chunk": {"chunkId": "c1", "value": "chunk text", "page": 3},
            }
        }
    ]


def test_unrelated_chunk_query_is_still_unsupported() -> None:
    with pytest.raises(UnsupportedGraphQueryError):
        _RecordingStore()._execute_query("// chunk-based something else MATCH (n) RETURN n", {})
