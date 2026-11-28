# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora PostgreSQL implementation of the LexicalGraphStore protocol.

Reads the graphrag-toolkit lexical graph that ``coa_common.graphrag_pg`` stores in two tables,
``<schema>.node`` and ``<schema>.edge``, through the RDS Data API. It answers the same seven
queries as :mod:`ndb_lexical` in SQL. The tenant is a bound parameter and lists travel as one JSON
string expanded in SQL (the Data API has no array parameters).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from coa_ontology.inducer.unstructured.stores.na_lexical import (
    _BATCH_SIZE,
    _extract_spc_predicate_complement,
    _extract_spo_predicate,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    RelationRecord,
    TopicRecord,
)

log = logging.getLogger(__name__)

# Same sink-side guard as NeptuneDatabaseLexicalStore: the tenant is always bound, but reject junk early.
_TENANT_ID_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


class AuroraLexicalStore:
    """LexicalGraphStore backed by the Aurora document graph (SQL via the RDS Data API)."""

    def __init__(self, client: Any = None, schema: str = "lg", tenant_id: str = "") -> None:
        """Bind the Data API client, the graph schema and the tenant.

        Args:
            client: An object with ``execute(sql, params) -> list[dict]`` (``AuroraDataApiClient``).
                Built lazily from ``AURORA_*`` when omitted.
            schema: Aurora schema holding the node/edge tables.
            tenant_id: Hex tenant suffix of the toolkit labels, or empty for the default tenant.

        Raises:
            ValueError: If ``schema`` is not a plain identifier or ``tenant_id`` is not hex.
        """
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid schema name {schema!r}")
        if tenant_id and not _TENANT_ID_HEX_RE.match(tenant_id):
            raise ValueError(f"tenant_id must be hex-only or empty; got {tenant_id!r}")
        self._client = client
        self._schema = schema
        self._tenant_id = tenant_id

    @property
    def client(self) -> Any:
        """The Data API client (built from the environment on first use)."""
        if self._client is None:
            from coa_common.aurora_data_api import build_client_from_env

            self._client = build_client_from_env()
        return self._client

    def _sql(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        bound = {"tenant": self._tenant_id, **(params or {})}
        return list(self.client.execute(sql.format(s=self._schema), bound))

    @staticmethod
    def _count(value: Any) -> int:
        return int(value) if value is not None else 0

    def get_class_nodes(self) -> list[ClassRecord]:
        """Return all class nodes with their occurrence counts, most frequent first."""
        rows = self._sql(
            "SELECT value, COALESCE((props->>'count')::bigint, 0) AS count FROM {s}.node "
            "WHERE label = '__SYS_Class__' AND tenant = :tenant ORDER BY count DESC, value"
        )
        return [
            ClassRecord(value=r["value"], count=self._count(r.get("count"))) for r in rows if r.get("value") is not None
        ]

    def get_class_relations(self) -> list[RelationRecord]:
        """Return the class-to-class relations with their occurrence counts."""
        rows = self._sql(
            "SELECT s.value AS source_class, r.value AS predicate, t.value AS target_class, "
            "COALESCE((r.props->>'count')::bigint, 0) AS count "
            "FROM {s}.edge r "
            "JOIN {s}.node s ON s.id = r.src AND s.label = '__SYS_Class__' AND s.tenant = :tenant "
            "JOIN {s}.node t ON t.id = r.dst AND t.label = '__SYS_Class__' AND t.tenant = :tenant "
            "WHERE r.type = '__SYS_RELATION__' ORDER BY s.value, r.value, t.value"
        )
        return [
            RelationRecord(
                source_class=r["source_class"],
                predicate=r["predicate"],
                target_class=r["target_class"],
                count=self._count(r.get("count")),
            )
            for r in rows
            if r.get("source_class") and r.get("predicate") and r.get("target_class")
        ]

    def get_entities_by_class(self, classification: str, limit: int = 100) -> list[EntityRecord]:
        """Return entities whose classification matches, up to a limit."""
        rows = self._sql(
            'SELECT id AS entity_id, value, "class" AS classification FROM {s}.node '
            "WHERE label = '__Entity__' AND tenant = :tenant AND \"class\" = :classification "
            "ORDER BY id LIMIT :limit",
            {"classification": classification, "limit": int(limit)},
        )
        return [
            EntityRecord(entity_id=r["entity_id"], value=r["value"], classification=r["classification"])
            for r in rows
            if r.get("entity_id") is not None and r.get("value") is not None
        ]

    def get_facts_for_entities(self, entity_ids: list[str]) -> list[FactRecord]:
        """Return the facts asserted about the given entities, batching the lookup."""
        if not entity_ids:
            return []
        facts: list[FactRecord] = []
        for i in range(0, len(entity_ids), _BATCH_SIZE):
            facts.extend(self._get_facts_batch(entity_ids[i : i + _BATCH_SIZE]))
        return facts

    def _get_facts_batch(self, entity_ids: list[str]) -> list[FactRecord]:
        rows = self._sql(
            'SELECT subj.value AS subject_value, subj."class" AS subject_class, f.value AS fact_value, '
            'obj.value AS object_value, obj."class" AS object_class '
            "FROM {s}.edge se "
            "JOIN {s}.node subj ON subj.id = se.src AND subj.label = '__Entity__' AND subj.tenant = :tenant "
            "JOIN {s}.node f ON f.id = se.dst AND f.label = '__Fact__' AND f.tenant = :tenant "
            "LEFT JOIN {s}.edge oe ON oe.type = '__OBJECT__' AND oe.dst = f.id "
            "LEFT JOIN {s}.node obj ON obj.id = oe.src AND obj.label = '__Entity__' AND obj.tenant = :tenant "
            "WHERE se.type = '__SUBJECT__' "
            "AND se.src IN (SELECT jsonb_array_elements_text(CAST(:entity_ids AS jsonb))) "
            "ORDER BY f.id, obj.id NULLS FIRST",
            {"entity_ids": list(entity_ids)},
        )
        facts: list[FactRecord] = []
        for row in rows:
            fact_value = row.get("fact_value")
            subj_value = row.get("subject_value")
            if not fact_value or not subj_value:
                continue
            has_object = row.get("object_value") is not None
            if has_object:
                predicate = _extract_spo_predicate(
                    fact_value=fact_value, subject_value=subj_value, object_value=row["object_value"]
                )
                complement = None
            else:
                predicate, complement = _extract_spc_predicate_complement(
                    fact_value=fact_value, subject_value=subj_value
                )
            if not predicate:
                continue
            facts.append(
                FactRecord(
                    subject_value=subj_value,
                    subject_class=row.get("subject_class") or "",
                    predicate=predicate,
                    object_value=row.get("object_value") if has_object else None,
                    object_class=row.get("object_class") if has_object else None,
                    complement=complement,
                )
            )
        return facts

    def get_topics(self) -> list[TopicRecord]:
        """Return all topic nodes with the count of statements belonging to each."""
        rows = self._sql(
            "SELECT t.id AS topic_id, t.value AS value, count(e.src) AS statement_count "
            "FROM {s}.node t "
            "LEFT JOIN {s}.edge e ON e.type = '__BELONGS_TO__' AND e.dst = t.id "
            "WHERE t.label = '__Topic__' AND t.tenant = :tenant "
            "GROUP BY t.id, t.value ORDER BY t.id"
        )
        return [
            TopicRecord(topic_id=r["topic_id"], value=r["value"], statement_count=self._count(r.get("statement_count")))
            for r in rows
            if r.get("topic_id") is not None and r.get("value") is not None
        ]

    def get_graph_schema(self) -> ClassSchemaResult:
        """Return the graph schema: its class nodes and their relations.

        Raises:
            ValueError: If the graph contains no class entries.
        """
        classes = self.get_class_nodes()
        if not classes:
            raise ValueError(
                "Graph contains zero class entries — not a valid lexical graph. "
                "The document ingest extracted no entity classes; re-ingest the document source."
            )
        return ClassSchemaResult(classes=classes, relations=self.get_class_relations())

    def health_check(self) -> dict:
        """Probe the Aurora connection with a trivial query."""
        try:
            self.client.execute("SELECT 1 AS ping", {})
            return {"status": "healthy", "backend": "aurora"}
        except Exception as e:
            return {"status": "unhealthy", "backend": "aurora", "error": str(e)}
