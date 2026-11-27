# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB-style expression engine and item <-> JSON encoding shared by the Aurora metadata backends.

``parse_condition`` / ``evaluate`` handle Condition, Filter and KeyCondition expressions; ``parse_update`` /
``apply_update`` handle UpdateExpression (``SET``, ``REMOVE``, ``ADD``). Only the forms the services use are
implemented; anything else raises ``NotImplementedError`` naming the token, so a new form fails loudly in a
test instead of being silently misread.

Sets are stored in JSON as ``{"__coa_set__": [...]}`` and numbers come back as ``Decimal``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

_SET_TAG = "__coa_set__"


def encode_value(value: Any) -> Any:
    """JSON-ready form of an item value: `Decimal` to int/float, sets to a tagged list."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return {_SET_TAG: sorted((encode_value(v) for v in value), key=repr)}
    if isinstance(value, dict):
        return {k: encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    return value


def decode_value(value: Any) -> Any:
    """Inverse of :func:ncode_value (tagged lists back to sets)."""
    if isinstance(value, dict):
        if set(value) == {_SET_TAG}:
            return {decode_value(v) for v in value[_SET_TAG]}
        return {k: decode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    return value


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


def attr_name(name: str, names: Mapping[str, str]) -> str:
    """Resolve a `#alias` through `names`; a plain attribute name is returned as is."""
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
    attr = attr_name(ref, names)
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
        attr = attr_name(act[1], names)
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
