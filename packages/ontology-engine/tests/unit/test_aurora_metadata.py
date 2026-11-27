# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Expression evaluator, selector and item encoding of the Aurora metadata shims."""

from __future__ import annotations

from decimal import Decimal

import pytest
from botocore.exceptions import ClientError
from coa_ontology import aurora_metadata as am

pytestmark = pytest.mark.unit

ITEM = {
    "PK": "ns#JOB#1",
    "SK": "STATUS",
    "status": "running",
    "acquired_ts": Decimal("100"),
    "job_id": "j1",
    "domain_tags": ["a", "b"],
    "ontology_type": "domain",
}

# Every ConditionExpression / FilterExpression form dynamo_store.py builds.
CONDITIONS = [
    (
        "attribute_not_exists(PK) OR attribute_not_exists(acquired_ts) OR acquired_ts < :stale",
        {":stale": 50},
        {},
        False,
    ),
    (
        "attribute_not_exists(PK) OR attribute_not_exists(acquired_ts) OR acquired_ts < :stale",
        {":stale": 500},
        {},
        True,
    ),
    ("job_id = :j", {":j": "j1"}, {}, True),
    ("job_id = :j", {":j": "other"}, {}, False),
    ("#s = :expected_status", {":expected_status": "running"}, {"#s": "status"}, True),
    ("#s = :expected_status", {":expected_status": "done"}, {"#s": "status"}, False),
    ("attribute_not_exists(#s) OR #s <> :forbidden_status", {":forbidden_status": "accepted"}, {"#s": "status"}, True),
    ("attribute_not_exists(#s) OR #s <> :forbidden_status", {":forbidden_status": "running"}, {"#s": "status"}, False),
    ("begins_with(PK, :pfx)", {":pfx": "ns#JOB#"}, {}, True),
    ("begins_with(PK, :pfx)", {":pfx": "zz"}, {}, False),
    (
        "begins_with(PK, :pk_pfx) AND SK = :sk AND #s = :st",
        {":pk_pfx": "ns#", ":sk": "STATUS", ":st": "running"},
        {"#s": "status"},
        True,
    ),
    ("(source_type = :src OR attribute_not_exists(source_type))", {":src": "X"}, {}, True),
    ("contains(domain_tags, :dt)", {":dt": "b"}, {}, True),
    ("contains(domain_tags, :dt)", {":dt": "z"}, {}, False),
    ("contains(PK, :m)", {":m": "#JOB#"}, {}, True),
    ("NOT attribute_exists(missing)", {}, {}, True),
    ("acquired_ts >= :v AND acquired_ts <= :v AND acquired_ts > :lo", {":v": 100, ":lo": 1}, {}, True),
]


@pytest.mark.parametrize(("expr", "values", "names", "expected"), CONDITIONS)
def test_conditions(expr, values, names, expected):
    assert am.evaluate(am.parse_condition(expr), ITEM, names, values) is expected


def test_missing_item_satisfies_attribute_not_exists():
    ast = am.parse_condition("attribute_not_exists(PK) OR attribute_not_exists(acquired_ts) OR acquired_ts < :stale")
    assert am.evaluate(ast, None, {}, {":stale": 1}) is True


@pytest.mark.parametrize(
    "expr",
    [
        "size(job_id) > :n",
        "job_id IN (:a, :b)",
        "job_id BETWEEN :a AND :b",
        "a.b = :v",
        "attribute_type(x, :t)",
        "job_id = :j ;",
    ],
)
def test_unknown_forms_raise_not_implemented(expr):
    with pytest.raises(NotImplementedError):
        am.parse_condition(expr)


UPDATES = [
    (
        "SET #s = :s, updated_at = :ts",
        {":s": "done", ":ts": "t"},
        {"#s": "status"},
        {"status": "done", "updated_at": "t"},
    ),
    ("SET updated_at = :ts", {":ts": "t"}, {}, {"updated_at": "t"}),
    ("SET #a0 = :v0 REMOVE #r0", {":v0": 1}, {"#a0": "n", "#r0": "status"}, {"n": 1}),
    (
        "SET source_proposals = list_append(if_not_exists(source_proposals, :e), :n) ADD class_count :c",
        {":e": [], ":n": ["p1"], ":c": 2},
        {},
        {"source_proposals": ["p1"], "class_count": 2},
    ),
]


@pytest.mark.parametrize(("expr", "values", "names", "expected"), UPDATES)
def test_updates(expr, values, names, expected):
    out = am.apply_update({"PK": "p", "SK": "s", "status": "x"}, am.parse_update(expr), names, values)
    for key, value in expected.items():
        assert out[key] == value
    if "REMOVE" in expr:
        assert "status" not in out


def test_add_accumulates_on_existing_number():
    out = am.apply_update(
        {"PK": "p", "SK": "s", "class_count": Decimal(3)}, am.parse_update("ADD class_count :c"), {}, {":c": 2}
    )
    assert out["class_count"] == 5


def test_list_append_extends_existing_list():
    out = am.apply_update(
        {"PK": "p", "SK": "s", "l": ["a"]},
        am.parse_update("SET l = list_append(if_not_exists(l, :e), :n)"),
        {},
        {":e": [], ":n": ["b"]},
    )
    assert out["l"] == ["a", "b"]


@pytest.mark.parametrize("expr", ["DELETE x :v", "SET a.b = :v", "SET a = size(b)", ""])
def test_unknown_update_forms_raise(expr):
    with pytest.raises(NotImplementedError):
        am.parse_update(expr)


def test_decimal_and_set_round_trip():
    item = {
        "PK": "p",
        "SK": "s",
        "ts": Decimal("1700000000.25"),
        "n": Decimal(3),
        "flag": True,
        "none": None,
        "tags": {"x", "y"},
        "nested": {"a": [Decimal("1.5"), {"k": Decimal(2)}]},
    }
    back = am.loads_item("p", "s", am.dumps_attributes(item))
    assert back == item
    assert isinstance(back["ts"], Decimal) and isinstance(back["n"], Decimal)
    assert back["tags"] == {"x", "y"}


def test_conditional_failure_is_the_dynamodb_error_code():
    err = am._conflict("PutItem")
    assert isinstance(err, ClientError)
    assert err.response["Error"]["Code"] == "ConditionalCheckFailedException"


class _Tx:
    def __init__(self, row):
        self.row = row
        self.writes = []

    def execute(self, sql, params=None):
        if sql.startswith("SELECT attributes"):
            return [{"a": am.dumps_attributes(self.row)}] if self.row else []
        if sql.startswith("INSERT") or sql.startswith("DELETE"):
            self.writes.append(sql)
        return []


class _Db:
    def __init__(self, row):
        self.tx = _Tx(row)

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield self.tx

        return cm()

    def execute(self, sql, params=None):
        return self.tx.execute(sql, params)


def _table(row):
    table = am.AuroraTable(client=_Db(row))
    table._ready = True
    return table


def test_failed_condition_writes_nothing_and_raises():
    table = _table({"PK": "p", "SK": "s", "job_id": "other"})
    with pytest.raises(ClientError) as exc:
        table.delete_item(
            Key={"PK": "p", "SK": "s"}, ConditionExpression="job_id = :j", ExpressionAttributeValues={":j": "me"}
        )
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert table._client.tx.writes == []


def test_passed_condition_writes():
    table = _table({"PK": "p", "SK": "s", "job_id": "me"})
    table.delete_item(
        Key={"PK": "p", "SK": "s"}, ConditionExpression="job_id = :j", ExpressionAttributeValues={":j": "me"}
    )
    assert len(table._client.tx.writes) == 1


def test_update_returns_all_new_and_creates_missing_item():
    table = _table(None)
    out = table.update_item(
        Key={"PK": "p", "SK": "s"},
        UpdateExpression="SET a = :v",
        ExpressionAttributeValues={":v": 1},
        ReturnValues="ALL_NEW",
    )
    assert out["Attributes"] == {"PK": "p", "SK": "s", "a": 1}


def test_query_without_pk_equality_is_rejected():
    with pytest.raises(NotImplementedError):
        _table(None).query(KeyConditionExpression="begins_with(SK, :s)", ExpressionAttributeValues={":s": "x"})


# ── selector ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metadata", "workbench", "expected"),
    [
        (None, None, "dynamodb"),
        (None, "neptune_opensearch", "dynamodb"),
        (None, "aurora_pgvector", "aurora"),
        ("dynamodb", "aurora_pgvector", "dynamodb"),
        ("aurora", None, "aurora"),
        ("AURORA", "neptune_opensearch", "aurora"),
    ],
)
def test_selector_precedence(monkeypatch, metadata, workbench, expected):
    for var, value in (("METADATA_BACKEND", metadata), ("WORKBENCH_BACKEND", workbench)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    assert am.metadata_backend() == expected


def test_selector_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("METADATA_BACKEND", "mongo")
    with pytest.raises(ValueError, match="METADATA_BACKEND"):
        am.metadata_backend()


def test_default_selector_keeps_dynamo_store_on_boto3(monkeypatch):
    from coa_ontology import dynamo_store as ds

    monkeypatch.delenv("METADATA_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    monkeypatch.setattr(ds, "_table", None)
    monkeypatch.setattr(ds, "_dao", None)
    table = ds._get_table()
    dao = ds._get_dao()
    assert not isinstance(table, am.AuroraTable)
    assert not isinstance(dao, am.AuroraDao)


def test_aurora_selector_gives_dynamo_store_the_shims(monkeypatch):
    from coa_ontology import dynamo_store as ds

    monkeypatch.setenv("METADATA_BACKEND", "aurora")
    monkeypatch.setattr(ds, "_table", None)
    monkeypatch.setattr(ds, "_dao", None)
    monkeypatch.setattr(am, "_table", None)
    monkeypatch.setattr(am, "_dao", None)
    assert isinstance(ds._get_table(), am.AuroraTable)
    assert isinstance(ds._get_dao(), am.AuroraDao)


# ── health probe ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("fails", "expected"), [(False, "ok"), (True, "error")])
def test_health_probe_uses_select_1_on_the_aurora_path(monkeypatch, fails, expected):
    from unittest.mock import MagicMock, patch

    from coa_ontology.stores.aurora import schema

    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    from coa_ontology import api_proxy_handler

    client = MagicMock()
    if fails:
        client.execute.side_effect = RuntimeError("boom")
    monkeypatch.setenv("METADATA_BACKEND", "aurora")
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")
    monkeypatch.delenv("SOURCES_TABLE", raising=False)
    monkeypatch.setattr(schema, "default_client", lambda: client)
    with (
        patch("boto3.client") as boto_client,
        patch.object(api_proxy_handler, "resolve_region", return_value="us-east-1"),
    ):
        result = api_proxy_handler.handler({"httpMethod": "GET", "resource": "/system-health"}, None)
    import json

    assert json.loads(result["body"])["dynamodb"] == expected
    client.execute.assert_called_once_with("SELECT 1")
    boto_client.assert_not_called()
