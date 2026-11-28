# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Teardown helpers for the Aurora document graph and its S3 Vectors indexes (the ckg path).

Used by the namespace-deletion pipeline and the graph-cleanup task. The Aurora side deletes a
tenant's ``node``/``edge`` rows through the Data API in bounded batches; the S3 Vectors side
probes for and drops the tenant's ``chunk``/``topic``/``statement`` indexes.
"""

from __future__ import annotations

import logging
from typing import Any

from coa_common.graphrag_pg.schema import _SCHEMA_RE

log = logging.getLogger(__name__)

DELETE_BATCH = 5000


def s3_vector_index_name(index: str, prefix: str) -> str:
    """Underlying S3 Vectors index name, as the toolkit builds it (``<prefix>.<name-with-dashes>``)."""
    name = index.replace("_", "-")
    return f"{prefix}.{name}" if prefix else name


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NotFoundException", "ResourceNotFoundException")


def delete_tenant_rows(client: Any, schema: str, tenant: str) -> dict[str, int]:
    """Delete every ``edge`` and ``node`` row of ``tenant`` in bounded batches. Safe to retry.

    Edges go first (matched by either endpoint), then nodes, so a retry after a partial run
    still finds the nodes it needs to locate the remaining edges.

    Raises:
        ValueError: If ``schema`` is not a valid schema name or ``tenant`` is empty. An empty
            tenant is the default tenant and must never be bulk-deleted here.
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")
    if not tenant:
        raise ValueError("tenant is required")
    s = schema
    edge_sql = (
        f"WITH d AS (DELETE FROM {s}.edge WHERE ctid IN "
        f"(SELECT e.ctid FROM {s}.edge e JOIN {s}.node n ON n.id = e.{{col}} "
        "WHERE n.tenant = :t LIMIT :batch) RETURNING 1) SELECT count(*) AS n FROM d"
    )
    statements = {
        "edge_src": edge_sql.format(col="src"),
        "edge_dst": edge_sql.format(col="dst"),
        "node": (
            f"WITH d AS (DELETE FROM {s}.node WHERE id IN "
            f"(SELECT id FROM {s}.node WHERE tenant = :t LIMIT :batch) RETURNING 1) SELECT count(*) AS n FROM d"
        ),
    }
    deleted: dict[str, int] = {}
    for kind, sql in statements.items():
        total = 0
        while True:
            rows = client.execute(sql, {"t": tenant, "batch": DELETE_BATCH})
            n = int(rows[0]["n"]) if rows else 0
            total += n
            if n < DELETE_BATCH:
                break
        deleted[kind] = total
    log.info("graphrag_pg tenant rows deleted tenant=%s deleted=%s", tenant, deleted)
    return deleted


def existing_s3_vector_indexes(s3vectors: Any, bucket: str, prefix: str, index_names: list[str]) -> list[str]:
    """Return the ``index_names`` whose S3 Vectors index exists (``GetIndex``).

    A failed probe other than not-found keeps the name, so a transient fault never skips a delete.
    """
    kept: list[str] = []
    for name in index_names:
        try:
            s3vectors.get_index(vectorBucketName=bucket, indexName=s3_vector_index_name(name, prefix))
            kept.append(name)
        except Exception as exc:  # noqa: BLE001 - probe only
            if _is_not_found(exc):
                log.info("s3 vectors index absent name=%s", name)
            else:
                log.warning("s3 vectors index probe failed, keeping name=%s", name, exc_info=True)
                kept.append(name)
    return kept


def delete_s3_vector_indexes(s3vectors: Any, bucket: str, prefix: str, index_names: list[str]) -> list[str]:
    """Delete each index (``DeleteIndex``). Returns those removed; an absent index is a no-op. Other errors raise."""
    removed: list[str] = []
    for name in index_names:
        try:
            s3vectors.delete_index(vectorBucketName=bucket, indexName=s3_vector_index_name(name, prefix))
            removed.append(name)
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise
    return removed
