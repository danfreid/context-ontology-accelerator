# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The boto3 ``Table`` adapter and the idempotency store under ``METADATA_BACKEND=aurora`` (in-memory store)."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from coa_common.dao import aurora_metadata_enabled, metadata_resource
from coa_common.idempotency import IdempotencyStore, reset_store_cache
from coa_common.testing_aurora import install

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    reset_store_cache()
    yield install(monkeypatch)
    reset_store_cache()


def _code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.response["Error"]["Code"]


def test_backend_switch(monkeypatch):
    monkeypatch.delenv("METADATA_BACKEND", raising=False)
    assert not aurora_metadata_enabled()
    monkeypatch.setenv("METADATA_BACKEND", "aurora")
    assert aurora_metadata_enabled()


def test_table_get_put_update_delete(store):
    table = metadata_resource().Table("coa-dev-sources")
    assert table.get_item(Key={"PK": "A", "SK": "B"}) == {}
    table.put_item(Item={"PK": "A", "SK": "B", "n": 1, "name": "x"})
    assert table.get_item(Key={"PK": "A", "SK": "B"})["Item"]["n"] == 1
    assert table.get_item(
        Key={"PK": "A", "SK": "B"}, ProjectionExpression="#n", ExpressionAttributeNames={"#n": "name"}
    )["Item"] == {"name": "x"}

    with pytest.raises(ClientError) as exc:
        table.put_item(Item={"PK": "A", "SK": "B"}, ConditionExpression="attribute_not_exists(PK)")
    assert _code(exc) == "ConditionalCheckFailedException"

    out = table.update_item(
        Key={"PK": "A", "SK": "B"},
        UpdateExpression="SET #n = #n + :one",
        ConditionExpression="#n = :cur",
        ExpressionAttributeNames={"#n": "n"},
        ExpressionAttributeValues={":one": 1, ":cur": 1},
        ReturnValues="ALL_NEW",
    )
    assert out["Attributes"]["n"] == 2
    with pytest.raises(ClientError) as exc:
        table.update_item(
            Key={"PK": "A", "SK": "B"},
            UpdateExpression="SET #n = :v",
            ConditionExpression="#n = :cur",
            ExpressionAttributeNames={"#n": "n"},
            ExpressionAttributeValues={":v": 9, ":cur": 1},
        )
    assert _code(exc) == "ConditionalCheckFailedException"

    with pytest.raises(ClientError):
        table.delete_item(Key={"PK": "A", "SK": "B"}, ConditionExpression="n = :x", ExpressionAttributeValues={":x": 7})
    table.delete_item(Key={"PK": "A", "SK": "B"})
    assert table.get_item(Key={"PK": "A", "SK": "B"}) == {}


def test_table_query_and_scan(store):
    table = metadata_resource().Table("coa-dev-sources")
    for i, status in enumerate(["COMPLETED", "REGISTERED", "COMPLETED"]):
        table.put_item(
            Item={
                "PK": "NS#1",
                "SK": f"SRC#{i}",
                "status": status,
                "namespaceId": "1",
                "createdAt": f"2026-01-0{i + 1}",
            }
        )
    table.put_item(
        Item={"PK": "NS#2", "SK": "SRC#9", "status": "COMPLETED", "namespaceId": "2", "createdAt": "2026-01-09"}
    )

    page = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
        ExpressionAttributeValues={":pk": "NS#1", ":p": "SRC#"},
        Limit=2,
    )
    assert [i["SK"] for i in page["Items"]] == ["SRC#0", "SRC#1"]
    rest = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
        ExpressionAttributeValues={":pk": "NS#1", ":p": "SRC#"},
        ExclusiveStartKey=page["LastEvaluatedKey"],
    )
    assert [i["SK"] for i in rest["Items"]] == ["SRC#2"]

    by_ns = table.query(
        IndexName="ByNamespace",
        KeyConditionExpression="namespaceId = :n",
        ExpressionAttributeValues={":n": "1"},
        ScanIndexForward=False,
        ProjectionExpression="SK",
    )
    assert [i["SK"] for i in by_ns["Items"]] == ["SRC#2", "SRC#1", "SRC#0"]

    scanned = table.scan(
        FilterExpression="namespaceId = :ns AND #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":ns": "1", ":s": "COMPLETED"},
        ProjectionExpression="PK,SK",
    )
    assert [i["SK"] for i in scanned["Items"]] == ["SRC#0", "SRC#2"]


def test_table_rejects_condition_objects(store):
    table = metadata_resource().Table("coa-dev-sources")
    with pytest.raises(NotImplementedError):
        table.query(KeyConditionExpression=object())


def test_nested_path_condition_and_if_not_exists(store):
    table = metadata_resource().Table("coa-dev-metric-import-jobs")
    table.put_item(Item={"PK": "NS#1", "SK": "IMPORT#j", "plan": {"offset": 5}, "done": [0, 4]})
    key = {"PK": "NS#1", "SK": "IMPORT#j"}
    names = {"#plan": "plan", "#o": "offset", "#d": "done", "#next": "nextOffset"}
    cond = "#plan.#o = :off AND NOT contains(#d, :off) AND attribute_not_exists(#next)"
    update = "SET #next = if_not_exists(#next, :off)"

    for wrong in (10, 4):  # 10 is not the planned offset; 4 is already processed
        with pytest.raises(ClientError):
            table.update_item(
                Key=key,
                UpdateExpression=update,
                ConditionExpression=cond,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={":off": wrong},
            )
    out = table.update_item(
        Key=key,
        UpdateExpression=update,
        ConditionExpression=cond,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={":off": 5},
        ReturnValues="ALL_NEW",
    )
    assert out["Attributes"]["nextOffset"] == 5


def test_idempotency_store_claim_take_over_complete_release(store):
    s = IdempotencyStore("coa-dev-idempotency")
    assert s.claim("k", operation="create", fingerprint="f", now=100)
    assert not s.claim("k", operation="create", fingerprint="f", now=101)
    assert s.get("k")["status"] == "IN_PROGRESS"

    assert not s.take_over("k", operation="create", fingerprint="f2", old_created_at=1, now=200)
    assert s.take_over("k", operation="create", fingerprint="f2", old_created_at=100, now=200)

    assert not s.complete("k", created_at=100, response={"statusCode": 201, "body": "{}"})
    assert s.complete("k", created_at=200, response={"statusCode": 201, "body": "{}", "headers": {"X": "1"}})
    record = s.get("k")
    assert (record["status"], record["status_code"], record["fingerprint"]) == ("COMPLETED", 201, "f2")

    s.release("k", created_at=999)  # not ours: no-op
    assert s.get("k") is not None
    s.release("k", created_at=200)
    assert s.get("k") is None
