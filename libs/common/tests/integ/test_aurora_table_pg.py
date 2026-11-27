# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AuroraMetadataTable (boto3 ``Table`` adapter) and the cache-version bump against real Postgres.

Run with a pgvector/pgvector:pg16 container: see ``test_dao_aurora_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError
from coa_common.dao.aurora import AuroraMetadataDao, AuroraMetadataTable

pytestmark = pytest.mark.integ

from test_aurora_search_pg import DSN, PgDataApi, _init_sql, psycopg  # noqa: E402

RUN = uuid.uuid4().hex[:8]
JOBS = f"coa-test-{RUN}-metric-import-jobs"
ROLES = f"coa-test-{RUN}-roles"
CACHE = f"coa-test-{RUN}-cache-invalidation"


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    with psycopg.connect(DSN) as c:
        c.execute(_init_sql())
        c.commit()
        yield c
        c.execute("DELETE FROM coa_metadata WHERE table_name LIKE %s", (f"coa-test-{RUN}-%",))
        c.commit()


def _table(conn: Any, name: str) -> AuroraMetadataTable:
    return AuroraMetadataTable(name, dao=AuroraMetadataDao(name, client=PgDataApi(conn)))


def test_import_job_claim_conditions_against_postgres(conn):
    table = _table(conn, JOBS)
    key = {"PK": "NS#1", "SK": "IMPORT#j"}
    table.put_item(Item={**key, "status": "IN_PROGRESS", "plan": {"offset": 5}, "done": [0, 4], "metricsProcessed": 5})
    names = {
        "#s": "status",
        "#plan": "plan",
        "#o": "offset",
        "#d": "done",
        "#next": "nextOffset",
        "#mp": "metricsProcessed",
    }
    cond = "#s = :ip AND #mp = :off AND #plan.#o = :off AND NOT contains(#d, :off) AND attribute_not_exists(#next)"
    update = "SET #next = if_not_exists(#next, :off)"

    with pytest.raises(ClientError) as lost:
        table.update_item(
            Key=key,
            UpdateExpression=update,
            ConditionExpression=cond,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={":ip": "IN_PROGRESS", ":off": 4},
        )
    assert lost.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    out = table.update_item(
        Key=key,
        UpdateExpression=update,
        ConditionExpression=cond,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={":ip": "IN_PROGRESS", ":off": 5},
        ReturnValues="ALL_NEW",
    )
    assert int(out["Attributes"]["nextOffset"]) == 5
    assert table.get_item(Key=key)["Item"]["nextOffset"] == 5


def test_put_item_does_not_add_timestamps_and_honours_condition(conn):
    table = _table(conn, JOBS)
    table.put_item(Item={"PK": "a", "SK": "b", "x": 1}, ConditionExpression="attribute_not_exists(PK)")
    assert set(table.get_item(Key={"PK": "a", "SK": "b"})["Item"]) == {"PK", "SK", "x"}
    with pytest.raises(ClientError):
        table.put_item(Item={"PK": "a", "SK": "b"}, ConditionExpression="attribute_not_exists(PK)")
    table.delete_item(Key={"PK": "a", "SK": "b"}, ConditionExpression="x = :x", ExpressionAttributeValues={":x": 1})
    assert table.get_item(Key={"PK": "a", "SK": "b"}) == {}


def test_writes_to_roles_bump_the_cache_version(conn, monkeypatch):
    monkeypatch.setenv("CACHE_INVALIDATION_TABLE_NAME", CACHE)
    roles = _table(conn, ROLES)
    cache = AuroraMetadataDao(CACHE, client=PgDataApi(conn))
    key = {"PK": "CACHE_VERSION", "SK": "CURRENT"}
    roles.put_item(Item={"PK": "NS#1", "SK": "ROLE#a"})
    roles.update_item(
        Key={"PK": "NS#1", "SK": "ROLE#a"},
        UpdateExpression="SET #n = :v",
        ExpressionAttributeNames={"#n": "n"},
        ExpressionAttributeValues={":v": 1},
    )
    roles.delete_item(Key={"PK": "NS#1", "SK": "ROLE#a"})
    assert int(cache.get(key)["version"]) == 3
    _table(conn, JOBS).put_item(Item={"PK": "z", "SK": "z"})  # other tables do not bump
    assert int(cache.get(key)["version"]) == 3
