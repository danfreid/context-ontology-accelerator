# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL implementation of the DatastoreDAO (``METADATA_BACKEND=aurora``).

Every logical DynamoDB table lives in one Postgres table, ``coa_metadata`` (see ``aurora-init.sql``):
``(table_name, pk, sk, attributes jsonb, ttl timestamptz)``. ``table_name`` is the name the caller passes
to :func:`coa_common.dao.make_dao`; it stays the logical key. ``attributes`` holds the whole item,
key attributes included, so a global secondary index is an expression index over ``attributes->>'attr'``.

Behaviour matches :class:`~coa_common.dao.dynamodb.DynamoDBDAO` where callers can observe it:

* a failed condition raises ``ClientError`` with code ``ConditionalCheckFailedException``; a failed
  :meth:`transact_write` raises ``TransactionCanceledException`` with ``CancellationReasons``;
* ``update`` creates a missing item, like ``UpdateItem``;
* items whose ``ttl`` (epoch seconds) has passed are not returned or counted;
* ``query`` and ``scan`` return at most ``limit`` items plus a ``last_evaluated_key`` while more rows
  remain, and stop a page at about 1 MB;
* numbers come back as ``Decimal``.

Atomicity: a conditional write, an update (read, modify, write) and a transaction run in one Data API
transaction that takes a per-item advisory lock and reads the row ``FOR UPDATE``, so two racers on the
same item serialise and exactly one wins a conditional put. Counters (:meth:`atomic_increment`,
:meth:`atomic_add`) are single ``INSERT .. ON CONFLICT DO UPDATE`` statements and do not look at ``ttl``.

Condition, filter, key-condition and update expressions use the subset implemented in
:mod:`coa_common.aurora_expressions`; anything else raises ``NotImplementedError``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from coa_common.aurora_data_api import AuroraDataApiClient, AuroraDataApiError, build_client_from_env
from coa_common.aurora_expressions import (
    apply_update,
    attr_name,
    decode_value,
    encode_value,
    evaluate,
    parse_condition,
    parse_update,
)
from coa_common.dao.base import DatastoreDAO, PaginatedResult, QueryParams

logger = logging.getLogger(__name__)

TABLE = "coa_metadata"

# index name -> (partition attribute, sort attribute or None). Each entry needs an expression index in
# aurora-init.sql (tests/unit/test_dao_aurora.py checks they match).
GSI_REGISTRY: dict[str, tuple[str, str | None]] = {
    "PrincipalIndex": ("principalKey", "resourceRoleKey"),
    "NamespaceGrantsIndex": ("namespaceKey", "principalRoleKey"),
    # sources-table sorts by createdAt; source-scan-jobs has the same index without a sort key, which
    # this registry entry serves too (its rows carry createdAt).
    "ByNamespace": ("namespaceId", "createdAt"),
    "BySourceType": ("namespaceId", "sourceTypeCreatedAt"),
    "ByName": ("namespaceId", "name"),
    "NameByIndex": ("name", None),
    "userId-lastActiveAt-index": ("userId", "lastActiveAt"),
    "userId-nsLastActiveAt-index": ("userId", "nsLastActiveAt"),
}

# Tables whose primary key is not (PK, SK), matched by table-name suffix (names carry a deployment prefix).
# A None sort attribute means a partition-key-only table (stored with sk = '').
KEY_SCHEMAS: dict[str, tuple[str, str | None]] = {
    "session-metadata": ("userId", "sessionId"),
    "idempotency": ("PK", None),
}
_DEFAULT_KEYS: tuple[str, str | None] = ("PK", "SK")

_ATTR = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_PAGE_BYTES = 1_000_000
_EVAL_BATCH = 200
_GET_CHUNK = 100
_DELETE_CHUNK = 500
_ALIVE = "(ttl IS NULL OR ttl > now())"


def key_schema(table_name: str) -> tuple[str, str | None]:
    """Primary key attribute names of a logical table: ``(partition attr, sort attr or None)``."""
    for suffix, schema in KEY_SCHEMAS.items():
        if table_name == suffix or table_name.endswith(f"-{suffix}"):
            return schema
    return _DEFAULT_KEYS


def _safe_attr(name: str) -> str:
    """An attribute name that may be written into SQL as a JSON key literal."""
    if not _ATTR.fullmatch(name):
        raise ValueError(f"unsupported attribute name {name!r}")
    return name


def _json_expr(attr: str) -> str:
    return f"(attributes->>'{_safe_attr(attr)}') COLLATE \"C\""


def _error(code: str, message: str, op: str, **extra: Any) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, **extra}, op)


def _conflict(op: str) -> ClientError:
    return _error("ConditionalCheckFailedException", "The conditional request failed", op)


def _dumps(item: Mapping[str, Any]) -> str:
    return json.dumps(encode_value(dict(item)), ensure_ascii=False)


def _loads(text: str) -> dict[str, Any]:
    return decode_value(json.loads(text, parse_float=Decimal, parse_int=Decimal))


def _ttl_epoch(item: Mapping[str, Any]) -> float | None:
    value = item.get("ttl")
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    return float(value)


def _plain(value: Any) -> Any:
    """A Data API parameter value for a key part."""
    return value if isinstance(value, str) else str(value)


class AuroraMetadataDao(DatastoreDAO):
    """One logical table of the ``coa_metadata`` store, behind the :class:`DatastoreDAO` interface.

    Args:
        table_name: The logical table name (the DynamoDB table name it replaces).
        key_attrs: ``(partition attr, sort attr or None)``; defaults to :func:`key_schema`.
        indexes: Extra or overriding GSI definitions merged over :data:`GSI_REGISTRY`.
        client: A Data API client; the process-wide one from the environment by default.
    """

    def __init__(
        self,
        table_name: str,
        *,
        key_attrs: tuple[str, str | None] | None = None,
        indexes: Mapping[str, tuple[str, str | None]] | None = None,
        client: AuroraDataApiClient | None = None,
    ) -> None:
        """Bind the DAO to a logical table; no connection is made until the first statement."""
        self._table_name = table_name
        self._pk_attr, self._sk_attr = key_attrs if key_attrs is not None else key_schema(table_name)
        self._indexes = {**GSI_REGISTRY, **(indexes or {})}
        self._client = client
        self._lock = threading.Lock()

    # ── plumbing ────────────────────────────────────────────────────

    def _db(self) -> AuroraDataApiClient:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = build_client_from_env()
        return self._client

    def _split(self, table_name: str, key: Mapping[str, Any]) -> tuple[str, str]:
        """``(pk, sk)`` column values for a key dict of ``table_name``."""
        pk_attr, sk_attr = (self._pk_attr, self._sk_attr) if table_name == self._table_name else key_schema(table_name)
        expected = {pk_attr} if sk_attr is None else {pk_attr, sk_attr}
        if set(key) != expected:
            raise ValueError(f"key of {table_name!r} must be exactly {sorted(expected)}, got {sorted(key)}")
        return str(key[pk_attr]), "" if sk_attr is None else str(key[sk_attr])

    def _split_item(self, table_name: str, item: Mapping[str, Any]) -> tuple[str, str]:
        pk_attr, sk_attr = (self._pk_attr, self._sk_attr) if table_name == self._table_name else key_schema(table_name)
        missing = [a for a in (pk_attr, sk_attr) if a is not None and a not in item]
        if missing:
            raise ValueError(f"item for {table_name!r} lacks key attribute(s) {missing}")
        return str(item[pk_attr]), "" if sk_attr is None else str(item[sk_attr])

    def _key_attrs(self, table_name: str, pk: str, sk: str) -> dict[str, str]:
        pk_attr, sk_attr = (self._pk_attr, self._sk_attr) if table_name == self._table_name else key_schema(table_name)
        return {pk_attr: pk} if sk_attr is None else {pk_attr: pk, sk_attr: sk}

    @staticmethod
    def _lock_item(tx: Any, table_name: str, pk: str, sk: str) -> dict[str, Any] | None:
        """Take the item's advisory lock and row lock inside ``tx``; the live item or ``None``."""
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))", {"k": f"{table_name}|{pk}|{sk}"})
        rows = tx.execute(
            f"SELECT attributes::text AS a, (ttl IS NOT NULL AND ttl <= now()) AS expired FROM {TABLE} "
            "WHERE table_name = :t AND pk = :pk AND sk = :sk FOR UPDATE",
            {"t": table_name, "pk": pk, "sk": sk},
        )
        if not rows or rows[0]["expired"]:
            return None
        return _loads(rows[0]["a"])

    @staticmethod
    def _write(tx: Any, table_name: str, pk: str, sk: str, item: Mapping[str, Any]) -> None:
        tx.execute(
            f"INSERT INTO {TABLE} (table_name, pk, sk, attributes, ttl) "
            "VALUES (:t, :pk, :sk, CAST(:a AS jsonb), to_timestamp(CAST(:ttl AS double precision))) "
            "ON CONFLICT (table_name, pk, sk) DO UPDATE SET attributes = EXCLUDED.attributes, ttl = EXCLUDED.ttl",
            {"t": table_name, "pk": pk, "sk": sk, "a": _dumps(item), "ttl": _ttl_epoch(item)},
        )

    @staticmethod
    def _check(
        condition: str | None,
        item: Mapping[str, Any] | None,
        names: Mapping[str, str] | None,
        values: Mapping[str, Any] | None,
    ) -> bool:
        if not condition:
            return True
        if not isinstance(condition, str):
            raise NotImplementedError("only string condition expressions are supported on the Aurora backend")
        return evaluate(parse_condition(condition), item, names or {}, values or {})

    # ── single item ─────────────────────────────────────────────────

    def get(
        self,
        key: dict[str, Any],
        *,
        projection: list[str] | None = None,
        consistent_read: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch a live item by primary key (``consistent_read`` is moot: every read is consistent)."""
        pk, sk = self._split(self._table_name, key)
        rows = self._db().execute(
            f"SELECT attributes::text AS a FROM {TABLE} WHERE table_name = :t AND pk = :pk AND sk = :sk AND {_ALIVE}",
            {"t": self._table_name, "pk": pk, "sk": sk},
        )
        if not rows:
            return None
        item = _loads(rows[0]["a"])
        return {k: item[k] for k in projection if k in item} if projection else item

    def get_attribute(self, key: dict[str, Any], attribute: str) -> Any | None:
        """Return a single attribute value, or ``None`` if the item or attribute is missing."""
        item = self.get(key, projection=[attribute])
        return item.get(attribute) if item else None

    def put(self, item: dict[str, Any], *, condition: str | None = None, auto_timestamp: bool = True) -> None:
        """Create or replace an item, optionally under a condition (``ConditionalCheckFailedException``)."""
        if auto_timestamp:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item.setdefault("createdAt", now)
            item["updatedAt"] = now
        pk, sk = self._split_item(self._table_name, item)
        if not condition:
            self._write(self._db(), self._table_name, pk, sk, item)
            return
        with self._db().transaction() as tx:
            current = self._lock_item(tx, self._table_name, pk, sk)
            if not self._check(condition, current, None, None):
                raise _conflict("PutItem")
            self._write(tx, self._table_name, pk, sk, item)

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
        """SET/REMOVE attributes, creating the item when absent. Same placeholder rules as DynamoDBDAO."""
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
        try:
            self._apply(key, expr, condition, names, values)
            return True
        except (ClientError, AuroraDataApiError):
            logger.warning(
                "Aurora metadata update failed",
                extra={"table": self._table_name, "key_attrs": list(key.keys()), "attributes": list(update_fields)},
                exc_info=True,
            )
            if raise_on_error:
                raise
            return False

    def _apply(
        self,
        key: Mapping[str, Any],
        update_expression: str,
        condition: str | None,
        names: Mapping[str, str],
        values: Mapping[str, Any],
    ) -> None:
        pk, sk = self._split(self._table_name, key)
        actions = parse_update(update_expression)
        with self._db().transaction() as tx:
            current = self._lock_item(tx, self._table_name, pk, sk)
            if not self._check(condition, current, names, values):
                raise _conflict("UpdateItem")
            base = current if current is not None else dict(key)
            updated = apply_update(base, actions, names, values)
            updated.update(key)
            self._write(tx, self._table_name, pk, sk, updated)

    def delete(self, key: dict[str, Any]) -> None:
        """Delete a single item by primary key."""
        pk, sk = self._split(self._table_name, key)
        self._db().execute(
            f"DELETE FROM {TABLE} WHERE table_name = :t AND pk = :pk AND sk = :sk",
            {"t": self._table_name, "pk": pk, "sk": sk},
        )

    # ── counters ────────────────────────────────────────────────────

    def _add(self, key: Mapping[str, Any], increments: Mapping[str, Any]) -> dict[str, Any]:
        """Single-statement upsert that adds each delta to its numeric attribute; returns the new attributes."""
        pk, sk = self._split(self._table_name, key)
        for attr in increments:
            _safe_attr(attr)
        inserted = {**key, **increments}
        rows = self._db().execute(
            f"INSERT INTO {TABLE} AS m (table_name, pk, sk, attributes) "
            "VALUES (:t, :pk, :sk, CAST(:ins AS jsonb)) "
            "ON CONFLICT (table_name, pk, sk) DO UPDATE SET attributes = m.attributes || ("
            "SELECT jsonb_object_agg(e.key, COALESCE((m.attributes->>e.key)::numeric, 0) + (e.value)::numeric) "
            "FROM jsonb_each_text(CAST(:inc AS jsonb)) AS e) "
            "RETURNING attributes::text AS a",
            {
                "t": self._table_name,
                "pk": pk,
                "sk": sk,
                "ins": _dumps(inserted),
                "inc": _dumps(dict(increments)),
            },
        )
        return _loads(rows[0]["a"])

    def atomic_increment(self, key: dict[str, Any], attribute: str, amount: int = 1) -> int:
        """Atomically add ``amount`` to a numeric attribute (created at 0) and return the new value."""
        return int(self._add(key, {attribute: amount})[attribute])

    def atomic_add(self, key: dict[str, Any], increments: dict[str, Any]) -> None:
        """Atomically ADD several numeric attributes in one statement; no-op when empty."""
        if increments:
            self._add(key, increments)

    # ── many items ──────────────────────────────────────────────────

    def _pairs(self, keys: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{"pk": pk, "sk": sk} for pk, sk in (self._split(self._table_name, k) for k in keys)]

    def batch_get(self, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fetch live items by key, 100 keys per statement (order not guaranteed)."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(keys), _GET_CHUNK):
            pairs = self._pairs(keys[i : i + _GET_CHUNK])
            rows = self._db().execute(
                f"SELECT m.attributes::text AS a FROM {TABLE} m "
                "JOIN jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(pk text, sk text) ON m.pk = k.pk AND m.sk = k.sk "
                "WHERE m.table_name = :t AND (m.ttl IS NULL OR m.ttl > now())",
                {"t": self._table_name, "keys": json.dumps(pairs)},
            )
            out.extend(_loads(r["a"]) for r in rows)
        return out

    def batch_delete(self, keys: list[dict[str, Any]]) -> None:
        """Delete items by key, 500 per statement. No-op when ``keys`` is empty."""
        for i in range(0, len(keys), _DELETE_CHUNK):
            pairs = self._pairs(keys[i : i + _DELETE_CHUNK])
            self._db().execute(
                f"DELETE FROM {TABLE} m USING jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(pk text, sk text) "
                "WHERE m.table_name = :t AND m.pk = k.pk AND m.sk = k.sk",
                {"t": self._table_name, "keys": json.dumps(pairs)},
            )

    def _read(
        self,
        *,
        where: list[str],
        params: dict[str, Any],
        order: list[str],
        forward: bool,
        cursor: list[Any] | None,
        limit: int | None,
        extra_select: str = "",
        evaluated_only: Callable[[dict[str, Any]], bool] | None = None,
        last_key: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        cursor_of: Callable[[dict[str, Any]], list[Any]],
    ) -> PaginatedResult:
        """Page through ``order`` with a keyset cursor, evaluating at most ``limit`` rows and about 1 MB."""
        direction = "ASC" if forward else "DESC"
        fetched: list[dict[str, Any]] = []
        size = 0
        more = False
        while True:
            n = _EVAL_BATCH if limit is None else min(_EVAL_BATCH, limit + 1 - len(fetched))
            conds = [f"table_name = :t AND {_ALIVE}", *where]
            call = {**params, "n": n}
            if cursor is not None:
                marks = ", ".join(f":cur{i}" for i in range(len(cursor)))
                conds.append(f"({', '.join(order)}) {'>' if forward else '<'} ({marks})")
                call.update({f"cur{i}": _plain(v) for i, v in enumerate(cursor)})
            rows = self._db().execute(
                f"SELECT pk, sk, attributes::text AS a{extra_select} FROM {TABLE} WHERE {' AND '.join(conds)} "
                f"ORDER BY {', '.join(f'{c} {direction}' for c in order)} LIMIT :n",
                call,
            )
            fetched.extend(rows)
            size += sum(len(r["a"]) + len(r["pk"]) + len(r["sk"]) for r in rows)
            if rows:
                cursor = cursor_of(rows[-1])
            if limit is not None and len(fetched) > limit:
                more = True
                break
            if len(rows) < n:
                break
            if size >= _PAGE_BYTES:
                more = True
                break
        if limit is not None:
            fetched = fetched[:limit]
        items: list[dict[str, Any]] = []
        for row in fetched:
            item = _loads(row["a"])
            if evaluated_only is None or evaluated_only(item):
                items.append(item)
        lek = last_key(fetched[-1], _loads(fetched[-1]["a"])) if more and fetched else None
        return PaginatedResult(items=items, last_evaluated_key=lek)

    def query(self, params: QueryParams) -> PaginatedResult:
        """Query one partition of the table or of a GSI (see :data:`GSI_REGISTRY`)."""
        names = params.expression_names or {}
        values = params.expression_values
        if params.index_name:
            if params.index_name not in self._indexes:
                raise NotImplementedError(f"index {params.index_name!r} is not in the Aurora GSI registry")
            part_attr, sort_attr = self._indexes[params.index_name]
        else:
            part_attr, sort_attr = self._pk_attr, self._sk_attr
        on_index = params.index_name is not None
        part_expr = _json_expr(part_attr) if on_index else "pk"
        sort_expr = (_json_expr(sort_attr) if on_index else "sk") if sort_attr else None

        ast = parse_condition(params.key_condition)
        conjuncts = list(ast[1:]) if ast[0] == "and" else [ast]
        where: list[str] = []
        sql_params: dict[str, Any] = {"t": self._table_name}
        have_part = False
        for node in conjuncts:
            kind = node[0]
            if kind == "cmp" and {node[2][0], node[3][0]} == {"path", "val"}:
                op = node[1]
                left, right = node[2], node[3]
                if left[0] == "val":
                    left, right = right, left
                    op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
                attr = attr_name(left[1], names)
                value = _plain(values[right[1]])
                if attr == part_attr and op == "=" and not have_part:
                    where.append(f"{part_expr} = :kc_pk")
                    sql_params["kc_pk"] = value
                    have_part = True
                    continue
                if sort_attr and attr == sort_attr and op in ("=", "<", "<=", ">", ">="):
                    where.append(f"{sort_expr} {op} :kc_sk{len(where)}")
                    sql_params[f"kc_sk{len(where) - 1}"] = value
                    continue
            if (
                kind == "fn"
                and node[1] == "begins_with"
                and node[2][0] == "path"
                and node[3][0] == "val"
                and sort_attr
                and attr_name(node[2][1], names) == sort_attr
            ):
                prefix = str(values[node[3][1]])
                where.append(f"{sort_expr} LIKE :kc_pfx ESCAPE '\\'")
                sql_params["kc_pfx"] = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                continue
            raise NotImplementedError(f"unsupported KeyConditionExpression part: {node!r}")
        if not have_part:
            raise NotImplementedError(f"a Query needs an equality condition on {part_attr!r}")
        if on_index and sort_attr:
            where.append(f"{sort_expr} IS NOT NULL")  # a GSI holds only items that carry its sort attribute

        base_pk, base_sk = self._pk_attr, self._sk_attr
        if on_index:
            order = ([sort_expr] if sort_expr else []) + ["pk", "sk"]
            extra = f", {sort_expr} AS isk" if sort_expr else ""
        else:
            order = ["sk"]
            extra = ""

        def cursor_of(row: dict[str, Any]) -> list[Any]:
            if on_index:
                return ([row["isk"]] if sort_expr else []) + [row["pk"], row["sk"]]
            return [row["sk"]]

        def last_key(row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
            key = self._key_attrs(self._table_name, row["pk"], row["sk"])
            if on_index:
                key[part_attr] = item[part_attr]
                if sort_attr:
                    key[sort_attr] = item[sort_attr]
            return key

        cursor: list[Any] | None = None
        start = params.exclusive_start_key
        if start:
            try:
                if on_index:
                    cursor = ([start[sort_attr]] if sort_attr else []) + [
                        start[base_pk],
                        str(start[base_sk]) if base_sk else "",
                    ]
                else:
                    cursor = [str(start[base_sk]) if base_sk else ""]
            except KeyError as exc:
                raise ValueError(f"exclusive_start_key lacks {exc.args[0]!r}") from exc
        return self._read(
            where=where,
            params=sql_params,
            order=order,
            forward=params.scan_forward,
            cursor=cursor,
            limit=params.limit or None,
            extra_select=extra,
            last_key=last_key,
            cursor_of=cursor_of,
        )

    def scan(
        self,
        *,
        filter_expression: str | None = None,
        expression_values: dict[str, Any] | None = None,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> PaginatedResult:
        """Scan the table in primary-key order. ``limit`` counts rows evaluated, like DynamoDB; the filter runs here."""
        ast = parse_condition(filter_expression) if filter_expression else None
        values = expression_values or {}
        cursor: list[Any] | None = None
        if exclusive_start_key:
            sk_attr = self._sk_attr
            cursor = [str(exclusive_start_key[self._pk_attr]), str(exclusive_start_key[sk_attr]) if sk_attr else ""]
        return self._read(
            where=[],
            params={"t": self._table_name},
            order=["pk", "sk"],
            forward=True,
            cursor=cursor,
            limit=limit or None,
            evaluated_only=(lambda item: evaluate(ast, item, {}, values)) if ast else None,
            last_key=lambda row, _item: self._key_attrs(self._table_name, row["pk"], row["sk"]),
            cursor_of=lambda row: [row["pk"], row["sk"]],
        )

    # ── transactions ────────────────────────────────────────────────

    def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Apply ``Put``/``Update``/``Delete``/``ConditionCheck`` items atomically (high-level attribute format).

        Every condition is evaluated against the rows as they were when the transaction locked them; if any
        fails nothing is written and ``TransactionCanceledException`` is raised with one
        ``CancellationReasons`` entry per item (``ConditionalCheckFailed`` or ``None``).
        """
        ops: list[tuple[str, str, str, str, dict[str, Any]]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in items:
            if len(entry) != 1:
                raise ValueError(f"a transact item has exactly one operation, got {sorted(entry)}")
            kind, spec = next(iter(entry.items()))
            if kind not in ("Put", "Update", "Delete", "ConditionCheck"):
                raise NotImplementedError(f"unsupported transact operation {kind!r}")
            table = spec["TableName"]
            pk, sk = self._split_item(table, spec["Item"]) if kind == "Put" else self._split(table, spec["Key"])
            if (table, pk, sk) in seen:
                raise _error(
                    "ValidationException",
                    "Transaction request cannot include multiple operations on one item",
                    "TransactWriteItems",
                )
            seen.add((table, pk, sk))
            ops.append((kind, table, pk, sk, spec))

        with self._db().transaction() as tx:
            current: dict[tuple[str, str, str], dict[str, Any] | None] = {}
            for table, pk, sk in sorted(seen):  # one global lock order, so two transactions never deadlock
                current[(table, pk, sk)] = self._lock_item(tx, table, pk, sk)
            reasons = []
            for _kind, table, pk, sk, spec in ops:
                ok = self._check(
                    spec.get("ConditionExpression"),
                    current[(table, pk, sk)],
                    spec.get("ExpressionAttributeNames"),
                    spec.get("ExpressionAttributeValues"),
                )
                reasons.append({"Code": "None" if ok else "ConditionalCheckFailed"})
            if any(r["Code"] != "None" for r in reasons):
                raise _error(
                    "TransactionCanceledException",
                    "Transaction cancelled, please refer cancellation reasons for specific reasons "
                    f"[{', '.join(r['Code'] for r in reasons)}]",
                    "TransactWriteItems",
                    CancellationReasons=reasons,
                )
            for kind, table, pk, sk, spec in ops:
                if kind == "Put":
                    self._write(tx, table, pk, sk, spec["Item"])
                elif kind == "Delete":
                    tx.execute(
                        f"DELETE FROM {TABLE} WHERE table_name = :t AND pk = :pk AND sk = :sk",
                        {"t": table, "pk": pk, "sk": sk},
                    )
                elif kind == "Update":
                    base = current[(table, pk, sk)]
                    if base is None:
                        base = dict(spec["Key"])
                    updated = apply_update(
                        base,
                        parse_update(spec["UpdateExpression"]),
                        spec.get("ExpressionAttributeNames") or {},
                        spec.get("ExpressionAttributeValues") or {},
                    )
                    updated.update(spec["Key"])
                    self._write(tx, table, pk, sk, updated)
