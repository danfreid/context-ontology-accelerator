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

log = logging.getLogger(__name__)

BACKEND_DYNAMODB = "dynamodb"
BACKEND_AURORA = "aurora"
# DynamoDB returns at most 1 MB per Query/Scan call; the shim stops a page at the same budget.
_PAGE_BYTES = 1_000_000
_EVAL_BATCH = 200
_SET_TAG = "__coa_set__"


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


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return {_SET_TAG: sorted((_encode(v) for v in value), key=repr)}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {_SET_TAG}:
            return {_decode(v) for v in value[_SET_TAG]}
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def dumps_attributes(item: Mapping[str, Any]) -> str:
    """JSON text of an item's non-key attributes."""
    return json.dumps(_encode({k: v for k, v in item.items() if k not in ("PK", "SK")}), ensure_ascii=False)


def loads_item(pk: str, sk: str, attributes_text: str) -> dict[str, Any]:
    """Rebuild a DynamoDB-shaped item (numbers as ``Decimal``)."""
    attrs = json.loads(attributes_text, parse_float=Decimal, parse_int=Decimal)
    return {"PK": pk, "SK": sk, **_decode(attrs)}


# ── expressions ─────────────────────────────────────────────────────────

_TOKEN = re.compile(r"\s*(?:(<>|<=|>=|=|<|>|\(|\)|,|\+|-)|(:[A-Za-z0-9_]+)|(#[A-Za-z0-9_]+)|([A-Za-z_][A-Za-z0-9_]*))")


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise NotImplementedError(f"unsupported token in expression at {text[pos:].strip()[:20]!r}: {text!r}")
        op, value, name, word = m.groups()
        if op:
            tokens.append(("op", op))
        elif value:
            tokens.append(("val", value))
        elif name:
            tokens.append(("name", name))
        else:
            tokens.append(("word", word))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise NotImplementedError(f"unexpected end of expression: {self.text!r}")
        self.i += 1
        return tok

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        tok = self.take()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise NotImplementedError(f"unsupported token {tok[1]!r} (expected {value or kind}) in {self.text!r}")
        return tok

    def at_word(self, *words: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "word" and tok[1].upper() in words

    def at_op(self, *ops: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "op" and tok[1] in ops

    def done(self) -> bool:
        return self.i >= len(self.tokens)

    # condition grammar
    def or_expr(self) -> tuple:
        parts = [self.and_expr()]
        while self.at_word("OR"):
            self.take()
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else ("or", *parts)

    def and_expr(self) -> tuple:
        parts = [self.not_expr()]
        while self.at_word("AND"):
            self.take()
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else ("and", *parts)

    def not_expr(self) -> tuple:
        if self.at_word("NOT"):
            self.take()
            return ("not", self.not_expr())
        return self.primary()

    def primary(self) -> tuple:
        if self.at_op("("):
            self.take()
            inner = self.or_expr()
            self.expect("op", ")")
            return inner
        tok = self.peek()
        nxt = self.tokens[self.i + 1] if self.i + 1 < len(self.tokens) else None
        if tok and tok[0] == "word" and nxt == ("op", "("):
            return self.function()
        left = self.operand()
        if not self.at_op("=", "<>", "<", "<=", ">", ">="):
            bad = self.peek()
            raise NotImplementedError(f"unsupported token {bad[1] if bad else 'end'!r} after operand in {self.text!r}")
        op = self.take()[1]
        return ("cmp", op, left, self.operand())

    _FUNCTIONS = {"attribute_exists": 1, "attribute_not_exists": 1, "begins_with": 2, "contains": 2}

    def function(self) -> tuple:
        name = self.take()[1]
        if name not in self._FUNCTIONS:
            raise NotImplementedError(f"unsupported function {name!r} in {self.text!r}")
        self.expect("op", "(")
        args = [self.operand()]
        while self.at_op(","):
            self.take()
            args.append(self.operand())
        self.expect("op", ")")
        if len(args) != self._FUNCTIONS[name]:
            raise NotImplementedError(f"{name} takes {self._FUNCTIONS[name]} arguments in {self.text!r}")
        return ("fn", name, *args)

    def operand(self) -> tuple:
        kind, value = self.take()
        if kind == "val":
            return ("val", value)
        if kind == "name":
            return ("path", value)
        if kind == "word":
            if self.at_op("(", ".") or value.upper() in {"AND", "OR", "NOT"}:
                raise NotImplementedError(f"unsupported expression near {value!r} in {self.text!r}")
            return ("path", value)
        raise NotImplementedError(f"unsupported token {value!r} in {self.text!r}")


def parse_condition(text: str) -> tuple:
    """Parse a Condition/Filter/KeyCondition expression into a tuple AST.

    Supported: ``AND``/``OR``/``NOT``, parentheses, ``= <> < <= > >=``, ``attribute_exists``,
    ``attribute_not_exists``, ``begins_with`` and ``contains``, over attribute names (``#alias`` or plain)
    and ``:value`` placeholders.
    """
    parser = _Parser(text)
    ast = parser.or_expr()
    if not parser.done():
        raise NotImplementedError(f"unsupported token {parser.peek()[1]!r} in {text!r}")  # type: ignore[index]
    return ast


def _resolve(operand: tuple, names: Mapping[str, str], values: Mapping[str, Any]) -> tuple[bool, Any]:
    """``(exists, value)`` of an operand; a path resolves against the item later."""
    raise NotImplementedError  # pragma: no cover - paths need the item; see _operand_value


def _attr(name: str, names: Mapping[str, str]) -> str:
    if name.startswith("#"):
        if name not in names:
            raise KeyError(f"expression attribute name {name} is not defined")
        return names[name]
    return name


def _operand_value(operand: tuple, item: Mapping[str, Any], names: Mapping[str, str], values: Mapping[str, Any]):
    kind, ref = operand
    if kind == "val":
        if ref not in values:
            raise KeyError(f"expression attribute value {ref} is not defined")
        return True, values[ref]
    attr = _attr(ref, names)
    return (attr in item), item.get(attr)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _compare(op: str, a: Any, b: Any) -> bool:
    if op == "=":
        return a == b and (_is_num(a) == _is_num(b))
    if op == "<>":
        return not (a == b and (_is_num(a) == _is_num(b)))
    comparable = (_is_num(a) and _is_num(b)) or (isinstance(a, str) and isinstance(b, str))
    if not comparable:
        return False
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate(ast: tuple, item: Mapping[str, Any] | None, names: Mapping[str, str], values: Mapping[str, Any]) -> bool:
    """Evaluate a parsed condition against an item (``None`` is a missing item).

    A comparison with a missing attribute is false, including ``<>``, as in DynamoDB.
    """
    row: Mapping[str, Any] = item or {}
    tag = ast[0]
    if tag == "or":
        return any(evaluate(a, item, names, values) for a in ast[1:])
    if tag == "and":
        return all(evaluate(a, item, names, values) for a in ast[1:])
    if tag == "not":
        return not evaluate(ast[1], item, names, values)
    if tag == "cmp":
        _, op, left, right = ast
        lexists, lval = _operand_value(left, row, names, values)
        rexists, rval = _operand_value(right, row, names, values)
        return lexists and rexists and _compare(op, lval, rval)
    if tag == "fn":
        fn = ast[1]
        if fn in ("attribute_exists", "attribute_not_exists"):
            exists, _ = _operand_value(ast[2], row, names, values)
            return exists if fn == "attribute_exists" else not exists
        lexists, lval = _operand_value(ast[2], row, names, values)
        rexists, rval = _operand_value(ast[3], row, names, values)
        if not (lexists and rexists):
            return False
        if fn == "begins_with":
            return isinstance(lval, str) and isinstance(rval, str) and lval.startswith(rval)
        if isinstance(lval, str):
            return isinstance(rval, str) and rval in lval
        return isinstance(lval, (list, set, frozenset, tuple)) and rval in lval
    raise NotImplementedError(f"unsupported condition node {tag!r}")  # pragma: no cover


def parse_update(text: str) -> list[tuple]:
    """Parse an UpdateExpression into ``[("SET", attr_or_alias, value_ast) | ("REMOVE", attr) | ("ADD", attr, value)]``.

    Supported value forms: ``:v``, ``path``, ``if_not_exists(path, value)``, ``list_append(a, b)`` and
    ``path + value`` / ``path - value``. ``DELETE`` and nested paths are not.
    """
    p = _Parser(text)
    actions: list[tuple] = []
    if p.done():
        raise NotImplementedError(f"empty UpdateExpression: {text!r}")

    def value() -> tuple:
        first = update_operand()
        if p.at_op("+", "-"):
            op = p.take()[1]
            return ("arith", op, first, update_operand())
        return first

    def update_operand() -> tuple:
        tok = p.peek()
        nxt = p.tokens[p.i + 1] if p.i + 1 < len(p.tokens) else None
        if tok and tok[0] == "word" and nxt == ("op", "("):
            fn = p.take()[1]
            if fn not in ("if_not_exists", "list_append"):
                raise NotImplementedError(f"unsupported function {fn!r} in {text!r}")
            p.expect("op", "(")
            a = update_operand() if fn == "list_append" else p.operand()
            p.expect("op", ",")
            b = update_operand()
            p.expect("op", ")")
            return (fn, a, b)
        return p.operand()

    while not p.done():
        clause = p.take()
        word = clause[1].upper() if clause[0] == "word" else clause[1]
        if word not in ("SET", "REMOVE", "ADD"):
            raise NotImplementedError(f"unsupported token {clause[1]!r} in UpdateExpression {text!r}")
        while True:
            target = p.take()
            if target[0] not in ("name", "word"):
                raise NotImplementedError(f"unsupported token {target[1]!r} in {text!r}")
            if p.at_op(".", "["):
                raise NotImplementedError(f"nested attribute paths are not supported: {text!r}")
            if word == "SET":
                p.expect("op", "=")
                actions.append(("SET", target[1], value()))
            elif word == "REMOVE":
                actions.append(("REMOVE", target[1]))
            else:
                actions.append(("ADD", target[1], p.operand()))
            if p.at_op(","):
                p.take()
                continue
            break
    return actions


def _eval_value(node: tuple, item: Mapping[str, Any], names: Mapping[str, str], values: Mapping[str, Any]) -> Any:
    tag = node[0]
    if tag in ("val", "path"):
        exists, val = _operand_value(node, item, names, values)
        if not exists:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": f"The provided expression refers to an attribute that does not exist: {node[1]}",
                    }
                },
                "UpdateItem",
            )
        return val
    if tag == "if_not_exists":
        exists, current = _operand_value(node[1], item, names, values)
        return current if exists else _eval_value(node[2], item, names, values)
    if tag == "list_append":
        return list(_eval_value(node[1], item, names, values)) + list(_eval_value(node[2], item, names, values))
    if tag == "arith":
        a = _eval_value(node[2], item, names, values)
        b = _eval_value(node[3], item, names, values)
        return a + b if node[1] == "+" else a - b
    raise NotImplementedError(f"unsupported update value {tag!r}")  # pragma: no cover


def apply_update(
    item: dict[str, Any], actions: list[tuple], names: Mapping[str, str], values: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a copy of ``item`` with ``actions`` applied."""
    out = dict(item)
    for act in actions:
        attr = _attr(act[1], names)
        if attr in ("PK", "SK"):
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Cannot update attribute PK/SK: part of the key"}},
                "UpdateItem",
            )
        if act[0] == "SET":
            out[attr] = _eval_value(act[2], item, names, values)
        elif act[0] == "REMOVE":
            out.pop(attr, None)
        else:  # ADD
            _, val = _operand_value(act[2], item, names, values)
            current = out.get(attr)
            if isinstance(val, (set, frozenset)):
                out[attr] = set(current or ()) | set(val)
            else:
                out[attr] = (current if current is not None else 0) + val
    return out


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
        """Use ``client`` (a ``DataApiClient``) or the process-wide one; the schema is checked on first use.

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
                    from coa_ontology.stores.aurora.schema import ensure_schema

                    ensure_schema(self._client, self._dimensions)
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
