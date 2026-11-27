# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in integration test of AuroraMetadataDao against a real local Postgres.

Same setup as ``test_aurora_search_pg.py`` (it supplies ``PgDataApi`` and the init script):

    COA_PG_TEST_DSN=postgresql://user:pw@localhost:5432/postgres pytest -m integ libs/common/tests/integ -k dao
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError
from coa_common.dao import QueryParams
from coa_common.dao.aurora import AuroraMetadataDao

pytestmark = pytest.mark.integ

# Importing this module applies its skip when COA_PG_TEST_DSN or psycopg is missing.
from test_aurora_search_pg import DSN, PgDataApi, _init_sql, psycopg  # noqa: E402

RUN = uuid.uuid4().hex[:8]
RRM = f"coa-test-{RUN}-rrm"
SOURCES = f"coa-test-{RUN}-sources"
IDEMPOTENCY = f"coa-test-{RUN}-idempotency"
SESSIONS = f"coa-test-{RUN}-session-metadata"


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    with psycopg.connect(DSN) as c:
        c.execute(_init_sql())
        c.commit()
        yield c
        c.execute("DELETE FROM coa_metadata WHERE table_name LIKE %s", (f"coa-test-{RUN}-%",))
        c.commit()


def _dao(conn: Any, table: str, **kwargs: Any) -> AuroraMetadataDao:
    return AuroraMetadataDao(table, client=PgDataApi(conn), **kwargs)


def _code(exc: pytest.ExceptionInfo[ClientError]) -> str:
    return exc.value.response["Error"]["Code"]


def test_put_get_roundtrip_keeps_types(conn):
    dao = _dao(conn, RRM)
    dao.put(
        {"PK": "k1", "SK": "s", "n": 3, "f": 1.5, "tags": {"a", "b"}, "nested": {"x": [1, 2]}, "flag": True},
        auto_timestamp=False,
    )
    item = dao.get({"PK": "k1", "SK": "s"})
    assert item == {
        "PK": "k1",
        "SK": "s",
        "n": Decimal(3),
        "f": Decimal("1.5"),
        "tags": {"a", "b"},
        "nested": {"x": [Decimal(1), Decimal(2)]},
        "flag": True,
    }
    assert dao.get({"PK": "k1", "SK": "s"}, projection=["n"]) == {"n": Decimal(3)}
    assert dao.get_attribute({"PK": "k1", "SK": "s"}, "n") == Decimal(3)
    assert dao.get({"PK": "k1", "SK": "missing"}) is None
    dao.delete({"PK": "k1", "SK": "s"})
    assert dao.get({"PK": "k1", "SK": "s"}) is None


def test_tables_are_isolated_by_logical_name(conn):
    a, b = _dao(conn, RRM), _dao(conn, SOURCES)
    a.put({"PK": "same", "SK": "same", "who": "a"}, auto_timestamp=False)
    b.put({"PK": "same", "SK": "same", "who": "b"}, auto_timestamp=False)
    assert a.get({"PK": "same", "SK": "same"})["who"] == "a"
    assert b.get({"PK": "same", "SK": "same"})["who"] == "b"


def test_conditional_put_race_has_exactly_one_winner(conn):
    """Eight connections race attribute_not_exists(PK) on one item; Postgres serialises them on the item lock."""
    table, key = f"coa-test-{RUN}-race", {"PK": "lock", "SK": "induction"}
    outcomes: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(8)

    def racer(n: int) -> None:
        with psycopg.connect(DSN) as c:
            dao = AuroraMetadataDao(table, client=PgDataApi(c))
            barrier.wait()
            try:
                dao.put({**key, "owner": n}, condition="attribute_not_exists(PK)")
                result = "won"
            except ClientError as exc:
                result = exc.response["Error"]["Code"]
            with guard:
                outcomes.append(result)

    threads = [threading.Thread(target=racer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("won") == 1
    assert outcomes.count("ConditionalCheckFailedException") == 7
    winner = _dao(conn, table).get(key)["owner"]
    assert isinstance(winner, Decimal)


def test_update_upserts_conditions_and_removes(conn):
    dao = _dao(conn, SOURCES)
    key = {"PK": "NS#1", "SK": "SRC#upd"}
    assert dao.update(key, {"status": "REGISTERED", "n": 1})
    assert dao.get(key)["status"] == "REGISTERED"
    assert dao.update(
        key,
        {"status": "APPROVED"},
        remove_fields=["n"],
        condition="#s = :old",
        condition_names={"#s": "status"},
        condition_values={":old": "REGISTERED"},
    )
    item = dao.get(key)
    assert item["status"] == "APPROVED" and "n" not in item
    with pytest.raises(ClientError) as exc:
        dao.update(
            key,
            {"status": "X"},
            condition="#s = :old",
            condition_names={"#s": "status"},
            condition_values={":old": "REGISTERED"},
        )
    assert _code(exc) == "ConditionalCheckFailedException"
    assert dao.update(key, {"status": "X"}, condition="attribute_not_exists(PK)", raise_on_error=False) is False
    assert dao.get(key)["status"] == "APPROVED"


def test_conditional_update_race_has_exactly_one_winner(conn):
    table, key = f"coa-test-{RUN}-claim", {"PK": "c", "SK": "c"}
    _dao(conn, table).put({**key, "state": "open"}, auto_timestamp=False)
    wins: list[int] = []
    guard = threading.Lock()
    barrier = threading.Barrier(6)

    def claim(n: int) -> None:
        with psycopg.connect(DSN) as c:
            dao = AuroraMetadataDao(table, client=PgDataApi(c))
            barrier.wait()
            if dao.update(key, {"owner": n}, condition="attribute_not_exists(owner)", raise_on_error=False):
                with guard:
                    wins.append(n)

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert _dao(conn, table).get(key)["owner"] == Decimal(wins[0])


def test_atomic_counters(conn):
    dao = _dao(conn, SOURCES)
    key = {"PK": "NS#1", "SK": "COUNTERS"}
    assert dao.atomic_increment(key, "documents", 2) == 2
    assert dao.atomic_increment(key, "documents", -1) == 1
    dao.atomic_add(key, {"documents": 4, "chunks": Decimal(10)})
    item = dao.get(key)
    assert item["documents"] == Decimal(5) and item["chunks"] == Decimal(10)
    assert item["PK"] == "NS#1"


def test_atomic_increment_is_atomic_under_concurrency(conn):
    table, key = f"coa-test-{RUN}-atomic", {"PK": "c", "SK": "c"}

    def bump() -> None:
        with psycopg.connect(DSN) as c:
            dao = AuroraMetadataDao(table, client=PgDataApi(c))
            for _ in range(10):
                dao.atomic_increment(key, "n")

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _dao(conn, table).get(key)["n"] == Decimal(60)


def _seed_grants(dao: AuroraMetadataDao) -> None:
    for i in range(7):
        dao.put(
            {
                "PK": f"NS#{i % 2}",
                "SK": f"GRANT#{i:02d}",
                "principalKey": "USER#alice",
                "resourceRoleKey": f"NS#{i % 2}#role-{i:02d}",
                "namespaceKey": f"NS#{i % 2}",
                "principalRoleKey": f"USER#alice#{i:02d}",
            },
            auto_timestamp=False,
        )
    dao.put(
        {"PK": "NS#9", "SK": "GRANT#x", "principalKey": "USER#bob", "resourceRoleKey": "NS#9#r"}, auto_timestamp=False
    )
    dao.put({"PK": "NS#9", "SK": "GRANT#nokey"}, auto_timestamp=False)  # no GSI attributes: absent from every index


def test_gsi_query_with_begins_with_and_ordering(conn):
    dao = _dao(conn, RRM)
    _seed_grants(dao)
    params = QueryParams(
        key_condition="principalKey = :pk AND begins_with(resourceRoleKey, :ns)",
        expression_values={":pk": "USER#alice", ":ns": "NS#1#"},
        index_name="PrincipalIndex",
    )
    items = dao.query(params).items
    assert [i["resourceRoleKey"] for i in items] == ["NS#1#role-01", "NS#1#role-03", "NS#1#role-05"]
    reverse = dao.query(QueryParams(**{**params.__dict__, "scan_forward": False})).items
    assert [i["resourceRoleKey"] for i in reverse] == list(reversed([i["resourceRoleKey"] for i in items]))
    only_pk = dao.query(
        QueryParams(
            key_condition="namespaceKey = :nk", expression_values={":nk": "NS#0"}, index_name="NamespaceGrantsIndex"
        )
    ).items
    assert len(only_pk) == 4
    assert (
        dao.query(
            QueryParams(
                key_condition="principalKey = :pk", expression_values={":pk": "USER#bob"}, index_name="PrincipalIndex"
            )
        ).items[0]["SK"]
        == "GRANT#x"
    )


def test_table_query_begins_with_and_comparisons(conn):
    dao = _dao(conn, RRM)
    _seed_grants(dao)
    got = dao.query(
        QueryParams(key_condition="PK = :pk AND begins_with(SK, :p)", expression_values={":pk": "NS#0", ":p": "GRANT#"})
    ).items
    assert [i["SK"] for i in got] == ["GRANT#00", "GRANT#02", "GRANT#04", "GRANT#06"]
    ge = dao.query(
        QueryParams(key_condition="PK = :pk AND SK >= :s", expression_values={":pk": "NS#0", ":s": "GRANT#03"})
    ).items
    assert [i["SK"] for i in ge] == ["GRANT#04", "GRANT#06"]


def test_query_pagination_covers_every_item_once_in_both_directions(conn):
    dao = _dao(conn, RRM)
    _seed_grants(dao)
    for forward in (True, False):
        params = QueryParams(
            key_condition="principalKey = :pk",
            expression_values={":pk": "USER#alice"},
            index_name="PrincipalIndex",
            limit=3,
            scan_forward=forward,
        )
        first = dao.query(params)
        assert len(first.items) == 3 and first.last_evaluated_key is not None
        everything = dao.query_all(params)
        keys = [(i["PK"], i["SK"]) for i in everything]
        assert len(keys) == 7 == len(set(keys))
        assert keys == sorted(
            keys,
            key=lambda k: next(i["resourceRoleKey"] for i in everything if (i["PK"], i["SK"]) == k),
            reverse=not forward,
        )
    exact = dao.query(QueryParams(key_condition="PK = :pk", expression_values={":pk": "NS#0"}, limit=4))
    assert len(exact.items) == 4 and exact.last_evaluated_key is None  # no spurious cursor when nothing follows


def test_query_pagination_with_equal_sort_values_does_not_skip_or_repeat(conn):
    dao = _dao(conn, SOURCES)
    for i in range(9):
        dao.put(
            {"PK": "NS#t", "SK": f"SRC#{i}", "namespaceId": "ns-tie", "createdAt": "2026-01-01T00:00:00Z"},
            auto_timestamp=False,
        )
    params = QueryParams(
        key_condition="namespaceId = :n", expression_values={":n": "ns-tie"}, index_name="ByNamespace", limit=2
    )
    keys = [i["SK"] for i in dao.query_all(params)]
    assert sorted(keys) == sorted(f"SRC#{i}" for i in range(9)) and len(keys) == 9


def test_name_index_and_name_uniqueness_lookup(conn):
    dao = _dao(conn, SOURCES)
    dao.put({"PK": "NS#n", "SK": "SRC#1", "namespaceId": "ns-name", "name": "orders"}, auto_timestamp=False)
    hit = dao.query(
        QueryParams(
            key_condition="namespaceId = :ns AND #n = :name",
            expression_values={":ns": "ns-name", ":name": "orders"},
            expression_names={"#n": "name"},
            index_name="ByName",
        )
    )
    assert [i["SK"] for i in hit.items] == ["SRC#1"]
    miss = dao.query(
        QueryParams(
            key_condition="namespaceId = :ns AND #n = :name",
            expression_values={":ns": "ns-name", ":name": "nope"},
            expression_names={"#n": "name"},
            index_name="ByName",
        )
    )
    assert miss.items == []


def test_session_table_uses_its_own_key_attributes_and_gsis(conn):
    dao = _dao(conn, SESSIONS)
    for i in range(3):
        dao.put(
            {
                "userId": "u1",
                "sessionId": f"s{i}",
                "lastActiveAt": f"2026-01-0{i + 1}T00:00:00Z",
                "nsLastActiveAt": f"ns#2026-01-0{i + 1}",
            },
            auto_timestamp=False,
        )
    latest = dao.query(
        QueryParams(
            key_condition="userId = :u",
            expression_values={":u": "u1"},
            index_name="userId-lastActiveAt-index",
            scan_forward=False,
            limit=1,
        )
    )
    assert latest.items[0]["sessionId"] == "s2"
    assert dao.get({"userId": "u1", "sessionId": "s0"})["sessionId"] == "s0"
    assert len(dao.query(QueryParams(key_condition="userId = :u", expression_values={":u": "u1"})).items) == 3


def test_partition_key_only_table(conn):
    dao = _dao(conn, IDEMPOTENCY)
    dao.put({"PK": "idem#1", "state": "IN_PROGRESS"}, condition="attribute_not_exists(PK)", auto_timestamp=False)
    with pytest.raises(ClientError) as exc:
        dao.put({"PK": "idem#1", "state": "again"}, condition="attribute_not_exists(PK)", auto_timestamp=False)
    assert _code(exc) == "ConditionalCheckFailedException"
    assert dao.get({"PK": "idem#1"})["state"] == "IN_PROGRESS"


def test_scan_with_filter_limit_and_cursor(conn):
    table = f"coa-test-{RUN}-scan"
    dao = _dao(conn, table)
    for i in range(10):
        dao.put({"PK": f"p{i:02d}", "SK": "s", "even": i % 2 == 0}, auto_timestamp=False)
    page = dao.scan(filter_expression="even = :t", expression_values={":t": True}, limit=4)
    assert [i["PK"] for i in page.items] == ["p00", "p02"]  # limit counts rows evaluated
    seen = list(page.items)
    cursor = page.last_evaluated_key
    while cursor:
        page = dao.scan(
            filter_expression="even = :t", expression_values={":t": True}, limit=4, exclusive_start_key=cursor
        )
        seen.extend(page.items)
        cursor = page.last_evaluated_key
    assert [i["PK"] for i in seen] == ["p00", "p02", "p04", "p06", "p08"]


def test_batch_get_and_delete(conn):
    table = f"coa-test-{RUN}-batch"
    dao = _dao(conn, table)
    keys = [{"PK": f"b{i}", "SK": "s"} for i in range(250)]
    for k in keys:
        dao.put({**k, "v": 1}, auto_timestamp=False)
    assert len(dao.batch_get(keys)) == 250
    assert len(dao.batch_get(keys[:3] + [{"PK": "absent", "SK": "s"}])) == 3
    dao.batch_delete(keys[:120])
    assert len(dao.batch_get(keys)) == 130
    dao.batch_delete([])


def test_transact_write_commits_all_or_nothing(conn):
    dao = _dao(conn, SOURCES)
    source = {"PK": "NS#tx", "SK": "SRC#1"}
    dao.put({**source, "status": "REGISTERED"}, auto_timestamp=False)
    ops = lambda claim_ns: [  # noqa: E731
        {
            "Put": {
                "TableName": SOURCES,
                "Item": {"PK": "CLAIM#tx", "SK": "CLAIM", "namespaceId": claim_ns},
                "ConditionExpression": "attribute_not_exists(PK) OR namespaceId = :ns",
                "ExpressionAttributeValues": {":ns": "tx"},
            }
        },
        {
            "Update": {
                "TableName": SOURCES,
                "Key": source,
                "UpdateExpression": "SET #st = :a REMOVE errorMessage",
                "ConditionExpression": "#st = :r",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {":a": "APPROVED", ":r": "REGISTERED"},
            }
        },
    ]
    dao.put({"PK": "CLAIM#tx", "SK": "CLAIM", "namespaceId": "someone-else"}, auto_timestamp=False)
    with pytest.raises(ClientError) as exc:
        dao.transact_write(ops("tx"))
    assert _code(exc) == "TransactionCanceledException"
    assert exc.value.response["CancellationReasons"] == [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}]
    assert dao.get(source)["status"] == "REGISTERED"  # the update did not land
    dao.delete({"PK": "CLAIM#tx", "SK": "CLAIM"})
    dao.transact_write(ops("tx"))
    assert dao.get(source)["status"] == "APPROVED"
    assert dao.get({"PK": "CLAIM#tx", "SK": "CLAIM"})["namespaceId"] == "tx"
    dao.transact_write(
        [
            {
                "ConditionCheck": {
                    "TableName": SOURCES,
                    "Key": source,
                    "ConditionExpression": "#st = :a",
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": {":a": "APPROVED"},
                }
            },
            {"Delete": {"TableName": SOURCES, "Key": {"PK": "CLAIM#tx", "SK": "CLAIM"}}},
        ]
    )
    assert dao.get({"PK": "CLAIM#tx", "SK": "CLAIM"}) is None


def test_transactions_in_opposite_order_do_not_deadlock(conn):
    table = f"coa-test-{RUN}-dl"
    _dao(conn, table).put({"PK": "a", "SK": "s"}, auto_timestamp=False)
    _dao(conn, table).put({"PK": "b", "SK": "s"}, auto_timestamp=False)
    errors: list[BaseException] = []

    def run(order: list[str]) -> None:
        try:
            with psycopg.connect(DSN) as c:
                dao = AuroraMetadataDao(table, client=PgDataApi(c))
                for _ in range(10):
                    dao.transact_write(
                        [
                            {
                                "Update": {
                                    "TableName": table,
                                    "Key": {"PK": k, "SK": "s"},
                                    "UpdateExpression": "SET n = :n",
                                    "ExpressionAttributeValues": {":n": 1},
                                }
                            }
                            for k in order
                        ]
                    )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(o,)) for o in (["a", "b"], ["b", "a"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []


def test_expired_ttl_rows_are_not_returned(conn):
    table = f"coa-test-{RUN}-ttl"
    dao = _dao(conn, table)
    past, future = time.time() - 3600, time.time() + 3600
    dao.put({"PK": "p", "SK": "old", "ttl": int(past), "namespaceId": "ttl-ns", "createdAt": "1"}, auto_timestamp=False)
    dao.put(
        {"PK": "p", "SK": "new", "ttl": int(future), "namespaceId": "ttl-ns", "createdAt": "2"}, auto_timestamp=False
    )
    dao.put({"PK": "p", "SK": "forever", "namespaceId": "ttl-ns", "createdAt": "3"}, auto_timestamp=False)
    assert dao.get({"PK": "p", "SK": "old"}) is None
    assert dao.get({"PK": "p", "SK": "new"}) is not None
    assert sorted(
        i["SK"] for i in dao.query(QueryParams(key_condition="PK = :p", expression_values={":p": "p"})).items
    ) == ["forever", "new"]
    assert sorted(
        i["SK"]
        for i in dao.query(
            QueryParams(key_condition="namespaceId = :n", expression_values={":n": "ttl-ns"}, index_name="ByNamespace")
        ).items
    ) == ["forever", "new"]
    assert sorted(i["SK"] for i in dao.scan().items) == ["forever", "new"]
    assert sorted(i["SK"] for i in dao.batch_get([{"PK": "p", "SK": s} for s in ("old", "new", "forever")])) == [
        "forever",
        "new",
    ]


def test_expired_row_counts_as_absent_for_conditions(conn):
    table = f"coa-test-{RUN}-ttl2"
    dao = _dao(conn, table)
    key = {"PK": "idem", "SK": "x"}
    dao.put({**key, "ttl": int(time.time() - 10), "state": "stale"}, auto_timestamp=False)
    dao.put({**key, "state": "fresh"}, condition="attribute_not_exists(PK)", auto_timestamp=False)
    assert dao.get(key)["state"] == "fresh"
    dao.put({**key, "ttl": int(time.time() - 10)}, auto_timestamp=False)
    assert dao.update(key, {"state": "x"}, condition="attribute_exists(PK)", raise_on_error=False) is False
