# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AuroraTable and AuroraDao against a real Postgres (through the Data API fake)."""

from __future__ import annotations

import threading
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_ontology import dynamo_store as ds
from coa_ontology.aurora_metadata import AuroraDao, AuroraTable

pytestmark = pytest.mark.integ


@pytest.fixture
def table(aurora_client):
    return AuroraTable(client=aurora_client, dimensions=8)


@pytest.fixture
def store(monkeypatch, table):
    monkeypatch.setattr(ds, "_table", table)
    monkeypatch.setattr(ds, "_dao", AuroraDao(table))
    monkeypatch.setattr(ds, "_get_s3", lambda: MagicMock())
    monkeypatch.setattr(ds, "_best_effort_delete_s3_prefix", lambda *a, **k: None)
    return ds


def test_item_round_trip_keeps_decimals_sets_and_nulls(table):
    item = {
        "PK": "p",
        "SK": "s",
        "n": Decimal("1.25"),
        "i": Decimal(7),
        "tags": {"a", "b"},
        "x": None,
        "l": [Decimal(1), "z"],
    }
    table.put_item(Item=item)
    assert table.get_item(Key={"PK": "p", "SK": "s"})["Item"] == item
    assert table.get_item(Key={"PK": "p", "SK": "nope"}) == {}


def test_conditional_put_rejects_second_writer(table):
    cond = "attribute_not_exists(PK)"
    table.put_item(Item={"PK": "p", "SK": "s", "v": 1}, ConditionExpression=cond)
    with pytest.raises(ClientError) as exc:
        table.put_item(Item={"PK": "p", "SK": "s", "v": 2}, ConditionExpression=cond)
    assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert table.get_item(Key={"PK": "p", "SK": "s"})["Item"]["v"] == Decimal(1)


def test_query_pages_by_key_without_gaps(table):
    for i in range(7):
        table.put_item(Item={"PK": "p", "SK": f"K#{i}"})
    table.put_item(Item={"PK": "q", "SK": "K#0"})
    kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :s)",
        "ExpressionAttributeValues": {":pk": "p", ":s": "K#"},
        "Limit": 3,
    }
    seen: list[str] = []
    while True:
        resp = table.query(**kwargs)
        seen += [i["SK"] for i in resp["Items"]]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    assert seen == [f"K#{i}" for i in range(7)]


def test_scan_projection_and_batch_delete(table):
    for i in range(3):
        table.put_item(Item={"PK": f"ns#{i}", "SK": "S", "extra": i})
    resp = table.scan(
        FilterExpression="begins_with(PK, :p)", ExpressionAttributeValues={":p": "ns#"}, ProjectionExpression="PK, SK"
    )
    assert resp["Items"] == [{"PK": f"ns#{i}", "SK": "S"} for i in range(3)]
    with table.batch_writer() as batch:
        for item in resp["Items"]:
            batch.delete_item(Key=item)
    assert table.scan(FilterExpression="begins_with(PK, :p)", ExpressionAttributeValues={":p": "ns#"})["Items"] == []


def test_unknown_expression_is_not_implemented(table):
    with pytest.raises(NotImplementedError):
        table.put_item(
            Item={"PK": "p", "SK": "s"}, ConditionExpression="size(a) > :n", ExpressionAttributeValues={":n": 1}
        )


def test_induction_lock_race_has_exactly_one_winner(store):
    results: list[str] = []
    barrier = threading.Barrier(6)

    def racer(i: int) -> None:
        barrier.wait()
        try:
            store.acquire_induction_lock("race", f"job-{i}")
            results.append("won")
        except store.InductionLockHeldError:
            results.append("held")

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["held"] * 5 + ["won"]


def test_concurrent_extend_loses_no_increment(store):
    store.put_ontology_registry("ns", "o", {"label": "x"})
    threads = [
        threading.Thread(target=lambda: store.extend_ontology_registry("ns", "o", add_class_count=1)) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_ontology_registry("ns", "o")["class_count"] == Decimal(8)


def test_dao_update_condition_failure(store):
    store.put_async_job("VALIDATE", "j", {"job_id": "j", "status": "running"}, namespace="ns")
    store.update_async_job("VALIDATE", "j", {"status": "completed"}, namespace="ns")
    assert store.get_async_job("VALIDATE", "j", namespace="ns")["status"] == "completed"
    dao = store._get_dao()
    key = {"PK": "ns#ASYNCJOB#VALIDATE#j", "SK": "STATUS"}
    with pytest.raises(ClientError):
        dao.update(
            key, {"status": "x"}, condition="#c = :c", condition_names={"#c": "status"}, condition_values={":c": "nope"}
        )
    assert (
        dao.update(
            key,
            {"status": "x"},
            condition="#c = :c",
            condition_names={"#c": "status"},
            condition_values={":c": "nope"},
            raise_on_error=False,
        )
        is False
    )
