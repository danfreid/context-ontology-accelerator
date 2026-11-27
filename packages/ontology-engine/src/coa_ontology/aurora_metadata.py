# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora-backed stand-ins for the DynamoDB single table that ``dynamo_store`` uses.

``dynamo_store`` is written against a boto3 ``Table`` (``get_item``, ``put_item``, ``update_item``,
``delete_item``, ``query``, ``scan``, ``batch_writer``) and three ``coa_common.DynamoDBDAO`` calls
(``get``, ``put``, ``update``). With ``METADATA_BACKEND=aurora`` it receives :class:`AuroraTable` and
:class:`AuroraDao` instead, so none of its 60 functions change.

Items live in the ``ontology_engine(pk, sk, attributes jsonb)`` table that ``aurora-init.sql`` creates.
The key attributes keep their DynamoDB names in the item dict (``PK``, ``SK``); every other attribute is
in ``attributes``. Numbers come back as ``Decimal`` (as boto3 returns them).

Only the expression forms ``dynamo_store`` uses are implemented (see :func:`parse_condition` and
:func:`parse_update`). Anything else raises ``NotImplementedError`` naming the token, so a new form fails
loudly in a test instead of being silently misread.

Atomicity: an unconditional get, put or delete is one statement. A conditional write, or an update (which
reads before it writes), runs in one transaction that takes a per-item advisory lock, reads the row
``FOR UPDATE``, evaluates the condition and writes. Two racers on the same item serialise on the lock, so
exactly one wins a conditional put (the induction lock). A failed condition raises the same botocore
``ClientError`` code (``ConditionalCheckFailedException``) that ``dynamo_store`` already catches.

The module imports nothing heavy; the Data API client is loaded on first use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from coa_common.aurora_data_api import AuroraQueryError
from coa_common.aurora_expressions import (
    apply_update,
    evaluate,
    parse_condition,
    parse_update,
)
from coa_common.aurora_expressions import attr_name as _attr
from coa_common.aurora_expressions import decode_value as _decode
from coa_common.aurora_expressions import encode_value as _encode

log = logging.getLogger(__name__)

BACKEND_DYNAMODB = "dynamodb"
BACKEND_AURORA = "aurora"
# DynamoDB returns at most 1 MB per Query/Scan call; the shim stops a page at the same budget.
_PAGE_BYTES = 1_000_000
_EVAL_BATCH = 200


def metadata_backend() -> str:
    """Where the engine's own table lives: ``dynamodb`` (default) or ``aurora``.

    ``METADATA_BACKEND`` wins. When it is unset the table follows ``WORKBENCH_BACKEND``: ``aurora_pgvector``
    selects Aurora, anything else keeps DynamoDB.

    Raises:
        ValueError: ``METADATA_BACKEND`` holds another value.
    """
    value = os.getenv("METADATA_BACKEND", "").strip().lower()
    if value in (BACKEND_DYNAMODB, BACKEND_AURORA):
        return value
    if value:
        raise ValueError(f"Unknown METADATA_BACKEND {value!r}. Expected one of: {BACKEND_DYNAMODB}, {BACKEND_AURORA}.")
    return (
        BACKEND_AURORA if os.getenv("WORKBENCH_BACKEND", "").strip().lower() == "aurora_pgvector" else BACKEND_DYNAMODB
    )


# ── item <-> JSON ───────────────────────────────────────────────────────
def dumps_attributes(item: Mapping[str, Any]) -> str:
    """JSON text of an item's non-key attributes."""
    return json.dumps(_encode({k: v for k, v in item.items() if k not in ("PK", "SK")}), ensure_ascii=False)


def loads_item(pk: str, sk: str, attributes_text: str) -> dict[str, Any]:
    """Rebuild a DynamoDB-shaped item (numbers as ``Decimal``)."""
    attrs = json.loads(attributes_text, parse_float=Decimal, parse_int=Decimal)
    return {"PK": pk, "SK": sk, **_decode(attrs)}


# ── SQL plumbing ────────────────────────────────────────────────────────


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _conflict(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}}, op
    )


def _key(key: Mapping[str, Any]) -> tuple[str, str]:
    if set(key) != {"PK", "SK"}:
        raise ValueError(f"item key must be exactly PK and SK, got {sorted(key)}")
    return str(key["PK"]), str(key["SK"])


def _conjuncts(ast: tuple | None) -> list[tuple]:
    if ast is None:
        return []
    return list(ast[1:]) if ast[0] == "and" else [ast]


class AuroraTable:
    """The slice of the boto3 ``Table`` API that ``dynamo_store`` calls, on Aurora."""

    def __init__(self, client: Any | None = None, dimensions: int | None = None):
        """Use ``client`` (an ``AuroraDataApiClient``) or the process-wide one; the schema is checked on first use.

        ``dimensions`` is the embedding width the shared schema check expects (the configured one by default).
        """
        self._client = client
        self._dimensions = dimensions
        self._ready = False
        self._lock = threading.Lock()

    def _db(self) -> Any:
        if self._client is None:
            from coa_ontology.stores.aurora.schema import default_client

            self._client = default_client()
        if not self._ready:
            with self._lock:
                if not self._ready:
                    from coa_ontology.stores.aurora.schema import check_schema

                    check_schema(self._client, self._dimensions)
                    self._ready = True
        return self._client

    @contextmanager
    def _locked(self, op: str, pk: str, sk: str) -> Iterator[tuple[Any, dict[str, Any] | None]]:
        """One transaction holding the item's advisory lock and row lock; yields ``(tx, current item)``."""
        db = self._db()
        with db.transaction() as tx:
            tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))", {"k": f"{pk}|{sk}"})
            rows = tx.execute(
                "SELECT attributes::text AS a FROM ontology_engine WHERE pk = :pk AND sk = :sk FOR UPDATE",
                {"pk": pk, "sk": sk},
            )
            yield tx, (loads_item(pk, sk, rows[0]["a"]) if rows else None)

    @staticmethod
    def _write(tx: Any, item: Mapping[str, Any]) -> None:
        tx.execute(
            "INSERT INTO ontology_engine (pk, sk, attributes) VALUES (:pk, :sk, CAST(:a AS jsonb)) "
            "ON CONFLICT (pk, sk) DO UPDATE SET attributes = EXCLUDED.attributes",
            {"pk": item["PK"], "sk": item["SK"], "a": dumps_attributes(item)},
        )

    @staticmethod
    def _check(condition: str | None, item: Mapping[str, Any] | None, kwargs: Mapping[str, Any], op: str) -> None:
        if not condition:
            return
        ok = evaluate(
            parse_condition(condition),
            item,
            kwargs.get("ExpressionAttributeNames") or {},
            kwargs.get("ExpressionAttributeValues") or {},
        )
        if not ok:
            raise _conflict(op)

    # ── single item ─────────────────────────────────────────────────

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch one item; the response has no ``Item`` key when it does not exist."""
        pk, sk = _key(kwargs["Key"])
        rows = self._db().execute(
            "SELECT attributes::text AS a FROM ontology_engine WHERE pk = :pk AND sk = :sk", {"pk": pk, "sk": sk}
        )
        return {"Item": loads_item(pk, sk, rows[0]["a"])} if rows else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        """Create or replace an item, optionally under a ConditionExpression."""
        item = kwargs["Item"]
        pk, sk = str(item["PK"]), str(item["SK"])
        condition = kwargs.get("ConditionExpression")
        if not condition:
            self._write(self._db(), {**item, "PK": pk, "SK": sk})
            return {}
        with self._locked("PutItem", pk, sk) as (tx, current):
            self._check(condition, current, kwargs, "PutItem")
            self._write(tx, {**item, "PK": pk, "SK": sk})
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        """Apply an UpdateExpression (creating the item when absent, as DynamoDB does)."""
        pk, sk = _key(kwargs["Key"])
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        actions = parse_update(kwargs["UpdateExpression"])
        with self._locked("UpdateItem", pk, sk) as (tx, current):
            self._check(kwargs.get("ConditionExpression"), current, kwargs, "UpdateItem")
            updated = apply_update(current or {"PK": pk, "SK": sk}, actions, names, values)
            self._write(tx, updated)
        return {"Attributes": updated} if kwargs.get("ReturnValues") == "ALL_NEW" else {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        """Delete an item, optionally under a ConditionExpression."""
        pk, sk = _key(kwargs["Key"])
        condition = kwargs.get("ConditionExpression")
        if not condition:
            self._db().execute("DELETE FROM ontology_engine WHERE pk = :pk AND sk = :sk", {"pk": pk, "sk": sk})
            return {}
        with self._locked("DeleteItem", pk, sk) as (tx, current):
            self._check(condition, current, kwargs, "DeleteItem")
            tx.execute("DELETE FROM ontology_engine WHERE pk = :pk AND sk = :sk", {"pk": pk, "sk": sk})
        return {}

    # ── many items ──────────────────────────────────────────────────

    def _read(self, kwargs: Mapping[str, Any], key_condition: tuple | None) -> dict[str, Any]:
        """Shared body of ``query`` and ``scan``: push key predicates to SQL, filter the rest here, page by key."""
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        filter_ast = parse_condition(kwargs["FilterExpression"]) if kwargs.get("FilterExpression") else None
        where: list[str] = []
        params: dict[str, Any] = {}
        rest: list[tuple] = []

        def pushdown(node: tuple) -> bool:
            kind = node[0]
            if kind == "cmp" and node[1] == "=" and node[2][0] in ("path", "val") and node[3][0] in ("path", "val"):
                left, right = node[2], node[3]
                if left[0] == "val":
                    left, right = right, left
                col = _attr(left[1], names) if left[0] == "path" else None
                if col in ("PK", "SK") and right[0] == "val":
                    where.append(f"{col.lower()} = :{col.lower()}_eq")
                    params[f"{col.lower()}_eq"] = str(values[right[1]])
                    return True
            if kind == "fn" and node[1] == "begins_with" and node[2][0] == "path" and node[3][0] == "val":
                col = _attr(node[2][1], names)
                if col in ("PK", "SK"):
                    where.append(f"{col.lower()} LIKE :{col.lower()}_pfx ESCAPE '\\'")
                    params[f"{col.lower()}_pfx"] = _like_prefix(str(values[node[3][1]]))
                    return True
            return False

        for node in _conjuncts(key_condition):
            if not pushdown(node):
                raise NotImplementedError(f"unsupported KeyConditionExpression part: {node!r}")
        for node in _conjuncts(filter_ast):
            if not pushdown(node):
                rest.append(node)
        start = kwargs.get("ExclusiveStartKey")
        limit = kwargs.get("Limit")
        projection = (
            [p.strip() for p in kwargs["ProjectionExpression"].split(",")]
            if kwargs.get("ProjectionExpression")
            else None
        )
        if projection and any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in projection):
            raise NotImplementedError(f"unsupported ProjectionExpression: {kwargs['ProjectionExpression']!r}")

        db = self._db()
        out: list[dict[str, Any]] = []
        evaluated = 0
        size = 0
        last_key: dict[str, str] | None = None
        cursor = (str(start["PK"]), str(start["SK"])) if start else None
        more = True
        while more:
            batch = _EVAL_BATCH if limit is None else min(_EVAL_BATCH, limit - evaluated)
            sql_where = list(where)
            call = dict(params)
            if cursor:
                sql_where.append('(pk COLLATE "C", sk COLLATE "C") > (:c_pk, :c_sk)')
                call.update({"c_pk": cursor[0], "c_sk": cursor[1]})
            call["n"] = batch
            rows = db.execute(
                "SELECT pk, sk, attributes::text AS a FROM ontology_engine "
                + ("WHERE " + " AND ".join(sql_where) + " " if sql_where else "")
                + 'ORDER BY pk COLLATE "C", sk COLLATE "C" LIMIT :n',
                call,
            )
            for r in rows:
                item = loads_item(r["pk"], r["sk"], r["a"])
                evaluated += 1
                size += len(r["a"]) + len(r["pk"]) + len(r["sk"])
                last_key = {"PK": r["pk"], "SK": r["sk"]}
                if all(evaluate(node, item, names, values) for node in rest):
                    out.append({k: item[k] for k in projection if k in item} if projection else item)
            cursor = (rows[-1]["pk"], rows[-1]["sk"]) if rows else cursor
            more = len(rows) == batch
            if limit is not None and evaluated >= limit:
                break
            if size >= _PAGE_BYTES:
                break
        resp: dict[str, Any] = {"Items": out, "Count": len(out)}
        stopped_early = more or (limit is not None and evaluated >= limit)
        if stopped_early and last_key:
            resp["LastEvaluatedKey"] = last_key
        return resp

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Single-partition query: ``PK = :pk`` plus an optional sort-key condition."""
        key_ast = parse_condition(kwargs["KeyConditionExpression"])
        names = kwargs.get("ExpressionAttributeNames") or {}
        if not any(
            n[0] == "cmp"
            and n[1] == "="
            and (n[2] == ("path", "PK") or (n[2][0] == "path" and _attr(n[2][1], names) == "PK"))
            for n in _conjuncts(key_ast)
        ):
            raise NotImplementedError("a Query needs an equality condition on PK")
        return self._read(kwargs, key_ast)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        """Table scan with a FilterExpression (key predicates run in SQL, the rest here)."""
        return self._read(kwargs, None)

    @contextmanager
    def batch_writer(self) -> Iterator[_BatchWriter]:
        """Collect puts/deletes and flush them in bounded statements on exit."""
        writer = _BatchWriter(self)
        yield writer
        writer.flush()


class _BatchWriter:
    _CHUNK = 500

    def __init__(self, table: AuroraTable):
        self._table = table
        self._deletes: list[dict[str, str]] = []
        self._puts: list[dict[str, Any]] = []

    def delete_item(self, Key: Mapping[str, Any]) -> None:  # noqa: N803 - boto3 keyword names
        """Queue a delete."""
        pk, sk = _key(Key)
        self._deletes.append({"pk": pk, "sk": sk})

    def put_item(self, Item: Mapping[str, Any]) -> None:  # noqa: N803
        """Queue a put."""
        self._puts.append(dict(Item))

    def flush(self) -> None:
        """Send everything queued."""
        db = self._table._db()
        for i in range(0, len(self._deletes), self._CHUNK):
            db.execute(
                "DELETE FROM ontology_engine o USING jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(pk text, sk text) "
                "WHERE o.pk = k.pk AND o.sk = k.sk",
                {"keys": json.dumps(self._deletes[i : i + self._CHUNK])},
            )
        for item in self._puts:
            self._table.put_item(Item=item)
        self._deletes, self._puts = [], []


class AuroraDao:
    """The ``coa_common.DynamoDBDAO`` calls ``dynamo_store`` makes (``get``, ``put``, ``update``, ``delete``)."""

    def __init__(self, table: AuroraTable | None = None):
        """Wrap an :class:`AuroraTable` (the shared one by default)."""
        self._table = table or aurora_table()

    def get(
        self, key: dict[str, Any], *, projection: list[str] | None = None, consistent_read: bool = False
    ) -> dict[str, Any] | None:
        """Fetch one item or ``None``."""
        item = self._table.get_item(Key=key).get("Item")
        if item is not None and projection:
            return {k: item[k] for k in projection if k in item}
        return item

    def put(self, item: dict[str, Any], *, condition: str | None = None, auto_timestamp: bool = True) -> None:
        """Create or replace an item (``createdAt``/``updatedAt`` stamped like the DynamoDB DAO)."""
        if auto_timestamp:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item.setdefault("createdAt", now)
            item["updatedAt"] = now
        kwargs: dict[str, Any] = {"Item": item}
        if condition:
            kwargs["ConditionExpression"] = condition
        self._table.put_item(**kwargs)

    def update(
        self,
        key: dict[str, Any],
        update_fields: dict[str, Any],
        *,
        remove_fields: list[str] | None = None,
        condition: str | None = None,
        condition_values: dict[str, Any] | None = None,
        condition_names: dict[str, str] | None = None,
        auto_timestamp: bool = True,
        raise_on_error: bool = True,
    ) -> bool:
        """SET/REMOVE attributes (same expression the DynamoDB DAO builds)."""
        if auto_timestamp and "updatedAt" not in update_fields:
            update_fields = {**update_fields, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        parts: list[str] = []
        for i, (field_name, value) in enumerate(update_fields.items()):
            names[f"#a{i}"] = field_name
            values[f":v{i}"] = value
            parts.append(f"#a{i} = :v{i}")
        expr = f"SET {', '.join(parts)}"
        if remove_fields:
            aliases = []
            for j, field_name in enumerate(remove_fields):
                names[f"#r{j}"] = field_name
                aliases.append(f"#r{j}")
            expr += f" REMOVE {', '.join(aliases)}"
        for name, value in (condition_names or {}).items():
            if name in names and names[name] != value:
                raise ValueError(f"condition_names alias '{name}' collides with SET alias")
            names[name] = value
        for name, value in (condition_values or {}).items():
            if name in values and values[name] != value:
                raise ValueError(f"condition_values placeholder '{name}' collides with SET placeholder")
            values[name] = value
        kwargs: dict[str, Any] = {
            "Key": key,
            "UpdateExpression": expr,
            "ExpressionAttributeValues": values,
            "ExpressionAttributeNames": names,
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        try:
            self._table.update_item(**kwargs)
            return True
        except (ClientError, AuroraQueryError):
            log.warning("Aurora metadata update failed", exc_info=True)
            if raise_on_error:
                raise
            return False

    def delete(self, key: dict[str, Any]) -> None:
        """Delete one item."""
        self._table.delete_item(Key=key)


_table: AuroraTable | None = None
_dao: AuroraDao | None = None
_lock = threading.RLock()


def aurora_table() -> AuroraTable:
    """The process-wide :class:`AuroraTable`."""
    global _table
    with _lock:
        if _table is None:
            _table = AuroraTable()
        return _table


def aurora_dao() -> AuroraDao:
    """The process-wide :class:`AuroraDao`."""
    global _dao
    with _lock:
        if _dao is None:
            _dao = AuroraDao(aurora_table())
        return _dao
