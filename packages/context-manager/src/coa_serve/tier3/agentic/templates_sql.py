# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQL variants of the Agentic Retriever's property-graph templates (Aurora document graph).

On the ckg path (``GRAPH_BACKEND=aurora`` and ``GRAPHRAG_VECTOR_BACKEND=s3vectors``) the
GraphRAG lexical graph lives in two Aurora tables written by ``coa_common.graphrag_pg``:
``<schema>.node(id, label, tenant, value, search_str, class, props)`` and
``<schema>.edge(type, src, dst, value, props)``. ``label`` is the base toolkit label
(``__Entity__``) and ``tenant`` the suffix the toolkit adds to labels of a non-default tenant, so
the Cypher tenant-scoped labels become ``label = '__Entity__' AND tenant = :tenant``.

Each template here answers the same question as its namesake in :mod:`..templates` /
:mod:`.tools.graph_tool` and returns the same column names, so the tools parse both alike.

Parameter discipline: every caller-influenced value (tenant, ids, terms, edge types, entity class,
source id) is a bound Data API parameter. Lists travel as one JSON string that SQL expands with
``jsonb_array_elements_text(CAST(:x AS jsonb))`` (the Data API has no array parameters). Only the
schema name (validated against a strict identifier pattern) and clamped integers (``limit``,
``hops``, ``entity_limit``, ``row_limit``) are formatted into the text. SQL text never contains
a literal ``{`` or ``}`` other than the ``str.format`` fields.

This module is dependency-free (no ``graphrag_toolkit``), like :mod:`..templates`.
"""

from __future__ import annotations

import re

from .templates import normalize_edge_type

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")

# SQL spelling of ``normalize_edge_type``: lowercase, then spaces and hyphens to underscores.
# MUST stay equivalent to the Python normaliser and to the Cypher ``_CYPHER_EDGE_NORM``.
_SQL_EDGE_NORM = "lower(replace(replace(r.value, ' ', '_'), '-', '_'))"

# ``normalize_edge_type`` is re-exported so callers (and tests) pair each SQL template with the one
# Python normaliser that feeds ``:edge_types_norm``.
__all__ = ["normalize_edge_type", "sql_schema"]


def sql_schema(schema: str) -> str:
    """Return ``schema`` after validating it as a plain SQL identifier (it is formatted into the text)."""
    if not isinstance(schema, str) or not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid Aurora graph schema name {schema!r}")
    return schema


def _like_pattern(value_sql: str) -> str:
    """SQL ``'%' || escaped(value) || '%' ESCAPE '!'`` so a bound term is matched literally by ``LIKE``.

    ``!`` is the escape character because the Data API's parameter scanner treats a backslash inside a
    string literal as an escape, which hides the closing quote and breaks every later ``:param``.
    """
    return f"'%' || replace(replace(replace({value_sql}, '!', '!!'), '%', '!%'), '_', '!_') || '%' ESCAPE '!'"


# Directed relation rows touching the seed set, with both endpoint nodes resolved. The two UNION arms
# let the planner use the (type, src, ...) primary key and the (type, dst, src) index instead of an OR.
_RELATION_ROWS = (
    "WITH seeds AS (SELECT jsonb_array_elements_text(CAST(:entity_ids AS jsonb)) AS id),\n"
    "rel AS (\n"
    "  SELECT r.type, r.src, r.dst, r.value, r.props FROM {schema}.edge r\n"
    "  WHERE r.type = '__RELATION__' AND r.src IN (SELECT id FROM seeds)\n"
    "  UNION\n"
    "  SELECT r.type, r.src, r.dst, r.value, r.props FROM {schema}.edge r\n"
    "  WHERE r.type = '__RELATION__' AND r.dst IN (SELECT id FROM seeds)\n"
    ")\n"
)

_CONNECTED_SELECT = (
    'SELECT r.src AS "sourceUri", s.value AS "sourceLabel",\n'
    '       r.props->>\'predicateUri\' AS "predicateUri", r.value AS "predicateLabel",\n'
    '       r.dst AS "targetUri", t.value AS "targetLabel",\n'
    '       CASE WHEN r.src IN (SELECT id FROM seeds) THEN r.dst ELSE r.src END AS "connectedUri",\n'
    '       CASE WHEN r.src IN (SELECT id FROM seeds) THEN t.value ELSE s.value END AS "connectedLabel",\n'
    '       CASE WHEN r.src IN (SELECT id FROM seeds) THEN t."class" ELSE s."class" END AS "connectedClass"\n'
    "FROM rel r\n"
    "JOIN {schema}.node s ON s.id = r.src AND s.label = '__Entity__' AND s.tenant = :tenant\n"
    "JOIN {schema}.node t ON t.id = r.dst AND t.label = '__Entity__' AND t.tenant = :tenant\n"
)

# ``:source_id`` -> original S3 key of the ``__Source__`` node.
SOURCE_KEY_LOOKUP: str = (
    "SELECT props->>'source_s3_key' AS key FROM {schema}.node\n"
    "WHERE label = '__Source__' AND tenant = :tenant AND id = :source_id\n"
    "LIMIT 1"
)

# find_documents: entity name -> mention path -> raw chunk and its source.
# ``:terms`` (JSON list of normalized terms), ``:entity_class`` ('' = any class).
ENTITY_MENTIONS: str = (
    "WITH matched AS (\n"
    '  SELECT t.term AS term, e.id AS id, e.value AS value, e."class" AS cls,\n'
    "         CASE WHEN e.search_str = t.term THEN 2 ELSE 1 END AS quality\n"
    "  FROM jsonb_array_elements_text(CAST(:terms AS jsonb)) AS t(term)\n"
    "  JOIN {schema}.node e ON e.label = '__Entity__' AND e.tenant = :tenant\n"
    "   AND (e.search_str = t.term OR e.search_str LIKE " + _like_pattern("t.term") + ")\n"
    "   AND (CAST(:entity_class AS text) = '' OR e.\"class\" = CAST(:entity_class AS text))\n"
    "  ORDER BY quality DESC, e.id, t.term\n"
    "  LIMIT {entity_limit}\n"
    ")\n"
    'SELECT DISTINCT m.term AS "term", m.quality AS "quality", m.value AS "label", m.cls AS "class",\n'
    '       c.id AS "chunk", c.value AS "text", src.id AS "source",\n'
    "       src.props->>'source_s3_key' AS \"key\"\n"
    "FROM matched m\n"
    "JOIN {schema}.edge sj ON sj.type IN ('__SUBJECT__', '__OBJECT__') AND sj.src = m.id\n"
    "JOIN {schema}.edge su ON su.type = '__SUPPORTS__' AND su.src = sj.dst\n"
    "JOIN {schema}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.src = su.dst\n"
    "JOIN {schema}.node c ON c.id = mi.dst AND c.label = '__Chunk__' AND c.tenant = :tenant\n"
    "JOIN {schema}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = c.id\n"
    "JOIN {schema}.node src ON src.id = ef.dst AND src.label = '__Source__' AND src.tenant = :tenant\n"
    "LIMIT {row_limit}"
)

# Edge-typed traversal: ``:entity_ids`` and ``:edge_types_norm`` (JSON lists).
EDGE_TYPED_TRAVERSAL: str = (
    _RELATION_ROWS.replace(
        "rel AS (", "types AS (SELECT jsonb_array_elements_text(CAST(:edge_types_norm AS jsonb)) AS v),\nrel AS (", 1
    )
    + "SELECT * FROM (\n"
    + _CONNECTED_SELECT
    + "WHERE "
    + _SQL_EDGE_NORM
    + " IN (SELECT v FROM types)\n"
    ") q\n"
    'ORDER BY "sourceUri", "targetUri", "predicateLabel"\n'
    "LIMIT {limit}"
)

# Supporting statements of the already-bounded connected entity set (one row per statement).
# ``DISTINCT`` for the same reason as the Cypher template: a statement supporting several facts of
# one entity would otherwise be returned once per fact.
CONNECTED_TEXT_RETRIEVAL: str = (
    'SELECT DISTINCT sj.src AS "connectedUri", st.value AS "connectedText"\n'
    "FROM {schema}.edge sj\n"
    "JOIN {schema}.node c ON c.id = sj.src AND c.label = '__Entity__' AND c.tenant = :tenant\n"
    "JOIN {schema}.edge su ON su.type = '__SUPPORTS__' AND su.src = sj.dst\n"
    "JOIN {schema}.node st ON st.id = su.dst AND st.label = '__Statement__' AND st.tenant = :tenant\n"
    "WHERE sj.type IN ('__SUBJECT__', '__OBJECT__')\n"
    "  AND sj.src IN (SELECT jsonb_array_elements_text(CAST(:connected_ids AS jsonb)))\n"
    'ORDER BY "connectedUri", "connectedText"\n'
    "LIMIT {limit}"
)

# ``uris`` mode: one hop over any ``__RELATION__`` edge, no edge-type constraint.
URI_EXPANSION: str = (
    _RELATION_ROWS + "SELECT * FROM (\n" + _CONNECTED_SELECT + ") q\n"
    'ORDER BY "sourceUri", "targetUri", "predicateLabel"\n'
    "LIMIT {limit}"
)

# ``edge_types`` mode: predicates present on the given entities with their frequency.
ENTITY_EDGE_TYPES: str = (
    _RELATION_ROWS + "SELECT r.value AS edge, count(*) AS freq\n"
    "FROM rel r\n"
    "JOIN {schema}.node s ON s.id = r.src AND s.label = '__Entity__' AND s.tenant = :tenant\n"
    "JOIN {schema}.node t ON t.id = r.dst AND t.label = '__Entity__' AND t.tenant = :tenant\n"
    "GROUP BY r.value\n"
    "ORDER BY freq DESC, r.value\n"
    "LIMIT {limit}"
)

# ``keyword`` mode: ``:terms`` are forward probes (``search_str`` contains the term).
KEYWORD_ENTITY_SEARCH: str = (
    'SELECT DISTINCT e.id AS uri, e.value AS label, e."class" AS "class"\n'
    "FROM jsonb_array_elements_text(CAST(:terms AS jsonb)) AS p(probe)\n"
    "JOIN {schema}.node e ON e.label = '__Entity__' AND e.tenant = :tenant\n"
    " AND e.search_str LIKE " + _like_pattern("p.probe") + "\n"
    "ORDER BY uri\n"
    "LIMIT {limit}"
)

# Adds the reverse arm: ``:containers`` are non-Latin script runs that CONTAIN a stored label. The
# floor counts characters of ``search_str`` without spaces (see the Cypher template for why).
KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS: str = (
    'SELECT DISTINCT e.id AS uri, e.value AS label, e."class" AS "class"\n'
    "FROM {schema}.node e\n"
    "WHERE e.label = '__Entity__' AND e.tenant = :tenant\n"
    "  AND (\n"
    "    EXISTS (SELECT 1 FROM jsonb_array_elements_text(CAST(:terms AS jsonb)) AS p(probe)\n"
    "            WHERE e.search_str LIKE " + _like_pattern("p.probe") + ")\n"
    "    OR (e.search_str IS NOT NULL AND char_length(replace(e.search_str, ' ', '')) >= 2\n"
    "        AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(CAST(:containers AS jsonb)) AS c(probe)\n"
    "                    WHERE strpos(c.probe, e.search_str) > 0))\n"
    "  )\n"
    "ORDER BY uri\n"
    "LIMIT {limit}"
)

# Chunk-window expansion over ``__NEXT__`` / ``__PREVIOUS__`` (undirected, up to ``{hops}`` hops).
# The seeds are excluded; the source join is OPTIONAL (older namespaces may lack it).
CHUNK_WINDOW_EXPANSION: str = (
    "WITH RECURSIVE walk(id, depth) AS (\n"
    "  SELECT c.id, 0 FROM {schema}.node c\n"
    "  WHERE c.label = '__Chunk__' AND c.tenant = :tenant\n"
    "    AND c.id IN (SELECT jsonb_array_elements_text(CAST(:chunk_ids AS jsonb)))\n"
    "  UNION\n"
    "  SELECT CASE WHEN e.src = w.id THEN e.dst ELSE e.src END, w.depth + 1\n"
    "  FROM walk w\n"
    "  JOIN {schema}.edge e ON e.type IN ('__NEXT__', '__PREVIOUS__') AND (e.src = w.id OR e.dst = w.id)\n"
    "  WHERE w.depth < {hops}\n"
    ")\n"
    'SELECT DISTINCT adj.id AS "chunkId", adj.value AS "text", src.id AS "sourceDocumentId",\n'
    "       src.props->>'source_s3_key' AS \"sourceS3Key\", src.props->>'filename' AS \"sourceFilename\"\n"
    "FROM walk w\n"
    "JOIN {schema}.node adj ON adj.id = w.id AND adj.label = '__Chunk__' AND adj.tenant = :tenant\n"
    "LEFT JOIN {schema}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = adj.id\n"
    "LEFT JOIN {schema}.node src ON src.id = ef.dst AND src.label = '__Source__' AND src.tenant = :tenant\n"
    "WHERE w.depth >= 1\n"
    "  AND adj.id NOT IN (SELECT jsonb_array_elements_text(CAST(:chunk_ids AS jsonb)))\n"
    'ORDER BY "chunkId"\n'
    "LIMIT {limit}"
)

SQL_TEMPLATES: dict[str, str] = {
    "SOURCE_KEY_LOOKUP": SOURCE_KEY_LOOKUP,
    "ENTITY_MENTIONS": ENTITY_MENTIONS,
    "EDGE_TYPED_TRAVERSAL": EDGE_TYPED_TRAVERSAL,
    "CONNECTED_TEXT_RETRIEVAL": CONNECTED_TEXT_RETRIEVAL,
    "URI_EXPANSION": URI_EXPANSION,
    "ENTITY_EDGE_TYPES": ENTITY_EDGE_TYPES,
    "KEYWORD_ENTITY_SEARCH": KEYWORD_ENTITY_SEARCH,
    "KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS": KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS,
    "CHUNK_WINDOW_EXPANSION": CHUNK_WINDOW_EXPANSION,
}
