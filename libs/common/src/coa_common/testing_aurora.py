# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-memory stand-in for the Aurora metadata store, for unit tests under ``METADATA_BACKEND=aurora``.

:class:`InMemoryMetadataDao` subclasses :class:`~coa_common.dao.aurora.AuroraMetadataDao` and replaces the SQL
with dictionaries, so the DAO surface, the expression parser/evaluator and the boto3-``Table`` adapter
(:class:`~coa_common.dao.aurora.AuroraMetadataTable`) behave as in production. Use :func:`install` to point
``make_dao`` and ``metadata_resource`` at one shared store. It does not emulate transactions across threads,
TTL expiry, or the 1 MB page cap; the real-Postgres tests in ``libs/common/tests/integ`` cover those.
"""

from __future__ import annotations

import copy
from typing import Any

from coa_common.aurora_expressions import apply_update, evaluate, parse_condition, parse_update
from coa_common.dao import aurora as _aurora
from coa_common.dao.aurora import AuroraMetadataDao, _conflict, _error
from coa_common.dao.base import PaginatedResult, QueryParams

Store = dict[str, dict[tuple[str, str], dict[str, Any]]]


class InMemoryMetadataDao(AuroraMetadataDao):
    """One logical table over a shared in-memory ``store`` (``{table: {(pk, sk): item}}``)."""

    def __init__(self, table_name: str, *, store: Store | None = None, **kwargs: Any) -> None:
        """Bind to ``table_name``; ``store`` is shared between DAOs so tables see each other."""
        kwargs.pop("client", None)
        super().__init__(table_name, **kwargs)
        self.store: Store = store if store is not None else {}

    # ── helpers ─────────────────────────────────────────────────────
    def _rows(self, table: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        return self.store.setdefault(table or self._table_name, {})

    def _bump_cache(self, *tables: str) -> None:
        if not any(t == s or t.endswith(f"-{s}") for t in tables for s in _aurora.CACHE_INVALIDATING_TABLES):
            return
        import os

        cache = os.environ.get("CACHE_INVALIDATION_TABLE_NAME", "cache-invalidation")
        InMemoryMetadataDao(cache, store=self.store).atomic_increment(
            {"PK": "CACHE_VERSION", "SK": "CURRENT"}, "version"
        )

    # ── single item ─────────────────────────────────────────────────
    def get(self, key, *, projection=None, consistent_read=False):  # type: ignore[no-untyped-def]  # noqa: D102
        item = self._rows().get(self._split(self._table_name, key))
        if item is None:
            return None
        item = copy.deepcopy(item)
        return {k: item[k] for k in projection if k in item} if projection else item

    def put(  # type: ignore[no-untyped-def]  # noqa: D102
        self, item, *, condition=None, auto_timestamp=True, condition_names=None, condition_values=None
    ):
        if auto_timestamp:
            item.setdefault("createdAt", "2026-01-01T00:00:00Z")
            item["updatedAt"] = "2026-01-01T00:00:00Z"
        key = self._split_item(self._table_name, item)
        if not self._check(condition, self._rows().get(key), condition_names, condition_values):
            raise _conflict("PutItem")
        self._rows()[key] = copy.deepcopy(item)
        self._bump_cache(self._table_name)

    def update_expression(self, key, update_expression, condition, names, values):  # type: ignore[no-untyped-def]  # noqa: D102
        k = self._split(self._table_name, key)
        current = self._rows().get(k)
        if not self._check(condition, current, names, values):
            raise _conflict("UpdateItem")
        base = copy.deepcopy(current) if current is not None else dict(key)
        updated = apply_update(base, parse_update(update_expression), names, values)
        updated.update(key)
        self._rows()[k] = updated
        self._bump_cache(self._table_name)
        return copy.deepcopy(updated)

    def delete(self, key):  # type: ignore[no-untyped-def]  # noqa: D102
        self._rows().pop(self._split(self._table_name, key), None)
        self._bump_cache(self._table_name)

    def delete_if(self, key, condition, names=None, values=None):  # type: ignore[no-untyped-def]  # noqa: D102
        k = self._split(self._table_name, key)
        if not self._check(condition, self._rows().get(k), names, values):
            raise _conflict("DeleteItem")
        self._rows().pop(k, None)
        self._bump_cache(self._table_name)

    # ── counters / many ─────────────────────────────────────────────
    def _add(self, key, increments):  # type: ignore[no-untyped-def]  # noqa: D102
        k = self._split(self._table_name, key)
        item = self._rows().setdefault(k, dict(key))
        for attr, delta in increments.items():
            item[attr] = item.get(attr, 0) + delta
        return copy.deepcopy(item)

    def batch_get(self, keys):  # type: ignore[no-untyped-def]  # noqa: D102
        out = [self._rows().get(self._split(self._table_name, k)) for k in keys]
        return [copy.deepcopy(i) for i in out if i is not None]

    def batch_delete(self, keys):  # type: ignore[no-untyped-def]  # noqa: D102
        for k in keys:
            self._rows().pop(self._split(self._table_name, k), None)
        if keys:
            self._bump_cache(self._table_name)

    # ── query / scan ────────────────────────────────────────────────
    def query(self, params: QueryParams) -> PaginatedResult:  # noqa: D102
        names = params.expression_names or {}
        ast = parse_condition(params.key_condition, nested_paths=True)
        if params.index_name:
            part, sort = self._indexes[params.index_name]
        else:
            part, sort = self._pk_attr, self._sk_attr
        rows = []
        for (pk, sk), item in self._rows().items():
            if params.index_name and (part not in item or (sort and sort not in item)):
                continue
            if evaluate(ast, item, names, params.expression_values):
                rows.append((pk, sk, item))
        rows.sort(key=lambda r: ((str(r[2].get(sort)) if sort else ""), r[0], r[1]), reverse=not params.scan_forward)
        if params.exclusive_start_key:
            start = params.exclusive_start_key
            ids = [
                self._split(self._table_name, {a: r[2][a] for a in self._key_attrs(self._table_name, r[0], r[1])})
                for r in rows
            ]
            target = self._split(self._table_name, {a: start[a] for a in self._key_attrs(self._table_name, "", "")})
            rows = rows[ids.index(target) + 1 :] if target in ids else rows
        lek = None
        if params.limit and len(rows) > params.limit:
            rows = rows[: params.limit]
            last = rows[-1]
            lek = self._key_attrs(self._table_name, last[0], last[1])
            if params.index_name:
                lek[part] = last[2][part]
                if sort:
                    lek[sort] = last[2][sort]
        return PaginatedResult(items=[copy.deepcopy(r[2]) for r in rows], last_evaluated_key=lek)

    def scan(  # type: ignore[no-untyped-def]  # noqa: D102
        self,
        *,
        filter_expression=None,
        expression_values=None,
        limit=None,
        exclusive_start_key=None,
        expression_names=None,
    ):
        ast = parse_condition(filter_expression) if filter_expression else None
        items = [copy.deepcopy(i) for _, i in sorted(self._rows().items())]
        if ast:
            items = [i for i in items if evaluate(ast, i, expression_names or {}, expression_values or {})]
        return PaginatedResult(items=items, last_evaluated_key=None)

    # ── transactions ────────────────────────────────────────────────
    def transact_write(self, items):  # type: ignore[no-untyped-def]  # noqa: D102
        snapshot = copy.deepcopy(self.store)
        reasons = []
        for entry in items:
            kind, spec = next(iter(entry.items()))
            table = spec["TableName"]
            pk_sk = self._split_item(table, spec["Item"]) if kind == "Put" else self._split(table, spec["Key"])
            ok = self._check(
                spec.get("ConditionExpression"),
                self._rows(table).get(pk_sk),
                spec.get("ExpressionAttributeNames"),
                spec.get("ExpressionAttributeValues"),
            )
            reasons.append({"Code": "None" if ok else "ConditionalCheckFailed"})
        if any(r["Code"] != "None" for r in reasons):
            raise _error(
                "TransactionCanceledException",
                "Transaction cancelled",
                "TransactWriteItems",
                CancellationReasons=reasons,
            )
        try:
            for entry in items:
                kind, spec = next(iter(entry.items()))
                table = spec["TableName"]
                if kind == "Put":
                    self._rows(table)[self._split_item(table, spec["Item"])] = copy.deepcopy(spec["Item"])
                elif kind == "Delete":
                    self._rows(table).pop(self._split(table, spec["Key"]), None)
                elif kind == "Update":
                    k = self._split(table, spec["Key"])
                    base = copy.deepcopy(self._rows(table).get(k) or dict(spec["Key"]))
                    updated = apply_update(
                        base,
                        parse_update(spec["UpdateExpression"]),
                        spec.get("ExpressionAttributeNames") or {},
                        spec.get("ExpressionAttributeValues") or {},
                    )
                    updated.update(spec["Key"])
                    self._rows(table)[k] = updated
        except Exception:
            self.store.clear()
            self.store.update(snapshot)
            raise
        self._bump_cache(*{next(iter(e.values()))["TableName"] for e in items})


def install(monkeypatch: Any) -> Store:
    """Switch the process to ``METADATA_BACKEND=aurora`` over one shared in-memory store; returns the store."""
    store: Store = {}
    monkeypatch.setenv("METADATA_BACKEND", "aurora")
    monkeypatch.setattr(
        _aurora, "AuroraMetadataDao", lambda table_name, **kw: InMemoryMetadataDao(table_name, store=store, **kw)
    )
    return store


__all__ = ["InMemoryMetadataDao", "install"]
