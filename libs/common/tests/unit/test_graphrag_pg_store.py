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
