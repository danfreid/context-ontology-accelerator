# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Golden fingerprints for the Aurora GraphRAG store: fail closed on any query it was not built for.

``graphrag_pg_driver`` runs the pinned toolkit's builders, every retrieval strategy COA enables,
``VersionManager``, ``DeleteSources`` and ``get_sources`` against a recording store. The canonical Cypher
of every query is compared with ``graphrag_pg_fingerprints.json``. A toolkit upgrade that adds or rewords a
query fails here, before it can reach a deployment. Regenerate the file after reviewing the new queries:

    GRAPHRAG_PG_UPDATE_FINGERPRINTS=1 pytest libs/common/tests/unit/test_graphrag_pg_golden.py
"""

from __future__ import annotations

# ruff: noqa: E501
import importlib.metadata
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("graphrag_toolkit")

from coa_common.graphrag_pg import store as pgstore  # noqa: E402
from coa_common.graphrag_pg.store import (  # noqa: E402
    UnsupportedGraphQueryError,
    canonicalize,
    classify,
    parse_write,
)
from graphrag_pg_driver import RecordingStore, run, unsupported_calls  # noqa: E402

FINGERPRINTS = Path(__file__).with_name("graphrag_pg_fingerprints.json")
PINNED_TOOLKIT = "3.20.0"
_VAR = re.compile(r"\bn[0-9a-f]{32}\b")
# Handlers added for the full strategy set. Each matches its query text exactly, so any rewording is refused.
_EXACT_HANDLERS = [
    name
    for name, _p, _h in pgstore._READ_HANDLERS[[n for n, _p, _h in pgstore._READ_HANDLERS].index("entity_multi") :]
    if name not in ("entities_for_keywords", "get_sources")
]


def _flat(cypher: str) -> str:
    return _VAR.sub("n<var>", " ".join(canonicalize(cypher)[0].split()))


def _computed() -> dict[str, Any]:
    flats = sorted(set(run()._flats))
    refused = sorted({_flat(c) for c, p in unsupported_calls() if _refused(c, p)})
    return {
        "toolkit_version": PINNED_TOOLKIT,
        "queries": [{"handler": classify(q), "query": q} for q in flats],
        "refused": refused,
    }


def _refused(cypher: str, params: dict[str, Any]) -> bool:
    try:
        RecordingStore(sql_mode=True)._execute_query(cypher, params)
    except UnsupportedGraphQueryError:
        return True
    return False


def test_toolkit_version_is_pinned() -> None:
    """Handlers are written against these exact query texts; a different toolkit must be re-audited."""
    assert importlib.metadata.version("graphrag-lexical-graph") == PINNED_TOOLKIT


def test_fingerprints_match_checked_in_file() -> None:
    computed = _computed()
    if os.environ.get("GRAPHRAG_PG_UPDATE_FINGERPRINTS"):
        FINGERPRINTS.write_text(json.dumps(computed, indent=1) + "\n", encoding="utf-8")
    assert json.loads(FINGERPRINTS.read_text(encoding="utf-8")) == computed


def test_every_fingerprint_has_a_handler_or_the_write_interpreter() -> None:
    unhandled = [q for q in sorted(set(run()._flats)) if classify(q) is None]
    assert unhandled == []


def test_every_write_query_parses_with_the_params_the_toolkit_sent() -> None:
    store = run()
    writes = 0
    for cypher, params in store._calls:
        if classify(cypher) != "write" or not params.get("params"):
            continue
        writes += 1
        plan = parse_write(cypher, params["params"])
        assert plan.nodes, _flat(cypher)
    assert writes > 10


def test_queries_coa_does_not_enable_are_refused() -> None:
    """``get_stats``, graph summary, domain labels and ``order_by`` stay unsupported by design."""
    refused = json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["refused"]
    assert len(refused) >= 8
    assert any("count(n) AS count" in q for q in refused)  # get_stats
    assert any("__SYS_Tenant__" in q for q in refused)  # graph summary cache
    assert any(" :`" in q for q in refused)  # include_domain_labels
    assert any("result.metadata.filename" in q for q in refused)  # get_sources order_by


def test_made_up_query_is_refused_and_not_retried() -> None:
    calls: list[str] = []

    class Counting(RecordingStore):
        def _execute_query(self, cypher, parameters=None, correlation_id=None):  # type: ignore[override]
            calls.append(cypher)
            return super()._execute_query(cypher, parameters, correlation_id)

    store = Counting(sql_mode=True)
    assert UnsupportedGraphQueryError in store.unretriable_exception_types()
    with pytest.raises(Exception, match="no SQL for query"):
        store.execute_query_with_retry("MATCH (n:`__Chunk__`) RETURN n.secret AS s", {}, max_attempts=5, max_wait=0)
    assert len(calls) == 1
    assert store._sql == []


def test_reworded_known_queries_are_refused_without_running_sql() -> None:
    queries = [
        q for q in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"] if q["handler"] in _EXACT_HANDLERS
    ]
    assert len(queries) >= 30
    for item in queries:
        store = RecordingStore(sql_mode=True)
        with pytest.raises(UnsupportedGraphQueryError):
            store._execute_query(item["query"] + " SKIP 1", {})
        assert store._sql == [], item["handler"]


def test_reworded_keyword_and_get_sources_queries_are_refused() -> None:
    queries = {q["handler"]: q["query"] for q in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]}
    for handler in ("entities_for_keywords", "get_sources"):
        reworded = queries[handler].replace("RETURN", "RETURN DISTINCT", 1)
        store = RecordingStore(sql_mode=True)
        with pytest.raises(UnsupportedGraphQueryError):
            store._execute_query(reworded, {})
        assert store._sql == []


def test_domain_labels_write_is_refused() -> None:
    cypher = "UNWIND $params AS params MERGE (n1:`__Entity__`{entityId: params.entityId}) SET n1 :`Company` // awsqid:e1-Company"
    with pytest.raises(UnsupportedGraphQueryError):
        RecordingStore(sql_mode=True)._execute_query(cypher, {"params": [{"entityId": "e1"}]})


# ── generated SQL, offline ───────────────────────────────────────────────────────────────────────
_PLACEHOLDER = re.compile(r"(?<!:):(?!:)([A-Za-z_]\w*)")


@pytest.fixture(scope="module")
def sql_store() -> RecordingStore:
    return run(sql_mode=True)


def test_every_statement_binds_exactly_the_placeholders_it_uses(sql_store: RecordingStore) -> None:
    assert len(sql_store._sql) > 100
    for kind, sql, params in sql_store._sql:
        used = set(_PLACEHOLDER.findall(sql))
        assert used <= set(params), (kind, sorted(used - set(params)))


def test_distinct_with_limit_has_a_tie_break_order_by(sql_store: RecordingStore) -> None:
    checked = 0
    for kind, sql, _params in sql_store._sql:
        if kind.startswith("read:") and "DISTINCT" in sql and re.search(r"\bLIMIT\b", sql):
            checked += 1
            assert re.search(r"ORDER BY", sql), kind
    assert checked >= 8


def test_no_read_with_limit_is_unordered(sql_store: RecordingStore) -> None:
    for kind, sql, _params in sql_store._sql:
        if kind.startswith("read:") and re.search(r"\bLIMIT\b", sql):
            assert "ORDER BY" in sql, kind


def test_edge_type_list_covers_every_relationship_the_toolkit_writes() -> None:
    types = set()
    for item in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]:
        types.update(re.findall(r"`(__[A-Z_]+__)`\]->", item["query"]))
        types.update(re.findall(r"\[:`(__[A-Z_]+__)`", item["query"]))
    assert types <= set(pgstore._EDGE_TYPES), sorted(types - set(pgstore._EDGE_TYPES))


class _Rows(RecordingStore):
    """SQL-mode store returning fixed rows for one statement kind."""

    def __init__(self, kind: str, rows: list[dict[str, Any]]) -> None:
        super().__init__(sql_mode=True)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_rows", rows)

    def _q(self, kind, sql, params=None):  # type: ignore[override]
        super()._q(kind, sql, params)
        return list(self._rows) if kind == self._kind else []


def _query(handler: str) -> str:
    for item in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]:
        if item["handler"] == handler:
            return item["query"]
    raise KeyError(handler)


def test_keyword_lookup_binds_tenant_and_escapes_like_wildcards() -> None:
    flat = next(
        i["query"]
        for i in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]
        if i["handler"] == "entities_for_keywords" and "STARTS WITH $keyword and entity.class STARTS WITH" in i["query"]
    )
    store = _Rows(
        "read:entities_for_keywords", [{"entityId": "e1", "value": "Acme_Co", "cls": "Company", "score": "4"}]
    )
    out = store._execute_query(
        flat.replace("`__Entity__`", "`__Entity__abc__`", 1), {"keyword": "acme_co%", "classification": "Comp"}
    )
    _kind, sql, params = store._sql[0]
    assert params == {"tenant": "abc", "kw": "acme\\_co\\%%", "cls": "Comp%"}
    assert "n.search_str LIKE :kw" in sql and "n.class LIKE :cls" in sql and "ORDER BY score DESC, n.id" in sql
    assert out == [{"result": {"entity": {"entityId": "e1", "value": "Acme_Co", "class": "Company"}, "score": 4}}]


def test_exact_keyword_lookup_excludes_local_entities() -> None:
    flat = next(
        i["query"]
        for i in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]
        if i["handler"] == "entities_for_keywords" and "search_str = $keyword AND entity.class <>" in i["query"]
    )
    store = _Rows("read:entities_for_keywords", [])
    store._execute_query(flat, {"keyword": "acme"})
    _kind, sql, params = store._sql[0]
    assert params == {"tenant": "", "kw": "acme"}
    assert "n.search_str = :kw" in sql and "n.class <> '__Local_Entity__'" in sql


def test_entity_multi_walks_two_relation_hops_and_expands_previous_statements() -> None:
    store = _Rows("read:entity_multi", [{"l": "st1"}, {"l": "st2"}])
    out = store._execute_query(_query("entity_multi"), {"startId": "e1", "endIds": ["e2", "e3"], "statementLimit": 12})
    _kind, sql, params = store._sql[0]
    assert out == [{"l": "st1"}, {"l": "st2"}]
    assert params["start"] == "e1" and json.loads(params["ids"]) == ["e2", "e3"]
    assert sql.count("'__RELATION__'") == 4  # one hop, then a second hop, each in both directions
    assert "'__PREVIOUS__'" in sql and "LIMIT 12" in sql and "ORDER BY l" in sql


def test_entity_multi_without_end_ids_runs_no_sql() -> None:
    store = _Rows("read:entity_multi", [])
    assert store._execute_query(_query("entity_multi"), {"startId": "e1", "endIds": [], "statementLimit": 5}) == []
    assert store._sql == []


def test_get_sources_filters_by_ids_tenant_and_metadata() -> None:
    flats = {
        i["query"]
        for i in json.loads(FINGERPRINTS.read_text(encoding="utf-8"))["queries"]
        if i["handler"] == "get_sources"
    }
    both = next(q for q in flats if "sourceIds" in q and "doc_source_id" in q)
    props = (
        '{"doc_source_id": "ds1", "__aws__versioning__valid_from__": 5, "__aws__versioning__prev_versions__": "a;b"}'
    )
    store = _Rows("read:get_sources", [{"sourceId": "src:1", "props": props}])
    out = store._execute_query(both.replace("`__Source__`", "`__Source__abc__`"), {"sourceIds": ["src:1"]})
    _kind, sql, params = store._sql[0]
    assert params["tenant"] == "abc" and params["ids"] == '["src:1"]' and params["fv0"] == "ds1"
    assert "n.id IN (SELECT jsonb_array_elements_text" in sql and "ORDER BY COALESCE" in sql
    result = out[0]["result"]
    assert result["sourceId"] == "src:1" and result["metadata"]["doc_source_id"] == "ds1"
    assert result["versioning"]["valid_from"] == 5 and result["versioning"]["prev_versions"] == ["a", "b"]
    assert result["versioning"]["valid_to"] == -1 and result["versioning"]["id_fields"] == [""]


def test_delete_handlers_scope_by_label_and_edge_type() -> None:
    store = RecordingStore(sql_mode=True)
    store._execute_query(_query("delete_entity_edges"), {"entityIds": ["e1"]})
    store._execute_query(_query("delete_entities"), {"entityIds": ["e1"]})
    store._execute_query(_query("delete_source"), {"sourceId": "src:1"})
    (_k1, edge_sql, _p1), (_k2, node_sql, _p2), (_k3, _src_sql, src_params) = store._sql
    assert (
        "DELETE FROM lg.edge" in edge_sql and "e.type IN (" in edge_sql and "e.src = q.id OR e.dst = q.id" in edge_sql
    )
    assert "DELETE FROM lg.node" in node_sql and "n.label = '__Entity__'" in node_sql
    assert src_params == {"ids": '["src:1"]'}


def test_delete_with_no_ids_runs_no_sql() -> None:
    store = RecordingStore(sql_mode=True)
    for handler, key in (("delete_facts", "factIds"), ("delete_topic_edges", "topicIds")):
        store._execute_query(_query(handler), {key: []})
    assert store._sql == []


def test_orphan_checks_only_report_nodes_without_outgoing_edges() -> None:
    store = _Rows("read:delete_ids", [])
    store._execute_query(_query("delete_get_orphan_facts"), {"factIds": ["f1"]})
    store._execute_query(_query("delete_get_orphan_entities"), {"entityIds": ["e1"]})
    facts_sql, ents_sql = store._sql[0][1], store._sql[1][1]
    assert "NOT EXISTS" in facts_sql and "e.type IN ('__SUPPORTS__')" in facts_sql and "'__Fact__'" in facts_sql
    assert "NOT EXISTS" in ents_sql and "'__SUBJECT__', '__OBJECT__'" in ents_sql and "'__Entity__'" in ents_sql
