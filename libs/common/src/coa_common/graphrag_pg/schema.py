# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL tables for the GraphRAG lexical graph (spike schema).

Two tables model the property graph the toolkit writes:

* ``node``: one row per node. ``id`` is globally unique because the toolkit already embeds the
  tenant in every node id. ``label`` is the base label (``__Chunk__``, ``__Entity__``...), ``tenant``
  the suffix the toolkit adds to labels for a non-default tenant. ``value``, ``search_str`` and
  ``class`` are the hot properties; every other property lives in ``props``.
* ``edge``: one row per relationship, unique on (type, src, dst, value), which is what
  ``MERGE (a)-[:T {value: v}]->(b)`` means.
"""

from __future__ import annotations

import re
from typing import Any

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


def ddl(schema: str = "lg") -> list[str]:
    """Return the DDL statements (one per Data API call) for ``schema``."""
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")
    s = schema
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"""CREATE TABLE IF NOT EXISTS {s}.node (
            id text PRIMARY KEY,
            label text NOT NULL,
            tenant text NOT NULL DEFAULT '',
            value text,
            search_str text,
            class text,
            props jsonb NOT NULL DEFAULT '{{}}'::jsonb
        )""",
        f"""CREATE TABLE IF NOT EXISTS {s}.edge (
            type text NOT NULL,
            src text NOT NULL,
            dst text NOT NULL,
            value text NOT NULL DEFAULT '',
            props jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (type, src, dst, value)
        )""",
        f"CREATE INDEX IF NOT EXISTS edge_dst_idx ON {s}.edge (type, dst, src)",
        f"CREATE INDEX IF NOT EXISTS node_label_idx ON {s}.node (label, tenant)",
        f"CREATE INDEX IF NOT EXISTS node_search_str_idx ON {s}.node (tenant, search_str text_pattern_ops) "
        "WHERE label = '__Entity__'",
        f"CREATE INDEX IF NOT EXISTS node_search_str_trgm_idx ON {s}.node USING gin (search_str gin_trgm_ops) "
        "WHERE label = '__Entity__'",
    ]


def ensure_schema(client: Any, schema: str = "lg") -> None:
    """Create the schema, tables and indexes when absent (idempotent)."""
    for stmt in ddl(schema):
        client.execute(stmt)
