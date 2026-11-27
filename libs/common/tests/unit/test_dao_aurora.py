# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for AuroraMetadataDao against a fake Data API client, and for make_dao."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO, QueryParams, make_dao
from coa_common.dao.aurora import GSI_REGISTRY, AuroraMetadataDao, key_schema

pytestmark = pytest.mark.unit

INIT_SQL = Path(__file__).resolve().parents[4] / "infra/lib/stacks/foundation/migrations/aurora-init.sql"

Responder = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class FakeClient:
    """Records every statement; ``respond`` supplies the rows. Transactions are tracked, not isolated."""

    def __init__(self, respond: Responder | None = None) -> None:
        self.respond = respond or (lambda sql, params: [])
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.transactions = 0
        self.rolled_back = 0

    def execute(self, sql: str, params: dict[str, Any] | None = None, *, tx: Any = None) -> list[dict[str, Any]]:
        self.statements.append((sql, dict(params or {})))
        return self.respond(sql, dict(params or {}))

    @contextmanager
    def transaction(self) -> Iterator[FakeClient]:
        self.transactions += 1
        try:
            yield self
        except BaseException:
            self.rolled_back += 1
            raise

    def sql(self) -> str:
        return "\n".join(s for s, _ in self.statements)


def _row(item: dict[str, Any], *, pk: str | None = None, sk: str = "", expired: bool = False, **extra: Any):
    return {"pk": pk or str(item["PK"]), "sk": sk, "a": json.dumps(item), "expired": expired, **extra}


def _locked(item: dict[str, Any] | None, expired: bool = False) -> Responder:
    """Responder that returns ``item`` for the FOR UPDATE read and nothing for everything else."""

    def respond(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "FOR UPDATE" in sql and item is not None:
            return [{"a": json.dumps(item), "expired": expired}]
        return []

    return respond


def test_make_dao_defaults_to_dynamodb(monkeypatch):
    monkeypatch.delenv("METADATA_BACKEND", raising=False)
    monkeypatch.setattr("coa_common.dao.DynamoDBDAO", lambda table, *, region, config=None: ("ddb", table, region))
    assert make_dao("t", region="us-east-1") == ("ddb", "t", "us-east-1")


@pytest.mark.parametrize("value", ["", "dynamodb", " DynamoDB "])
def test_make_dao_dynamodb_values(monkeypatch, value):
    monkeypatch.setenv("METADATA_BACKEND", value)
    dao = make_dao("t", region="us-east-1")
    assert isinstance(dao, DynamoDBDAO)


@pytest.mark.parametrize("value", ["aurora", "AURORA"])
def test_make_dao_aurora(monkeypatch, value):
    monkeypatch.setenv("METADATA_BACKEND", value)
    dao = make_dao("coa-dev-roles", region="us-east-1")
    assert isinstance(dao, AuroraMetadataDao)


def test_make_dao_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("METADATA_BACKEND", "mongo")
    with pytest.raises(ValueError, match="METADATA_BACKEND"):
        make_dao("t", region="us-east-1")


def test_make_dao_exported_from_coa_common():
    import coa_common

    assert coa_common.make_dao is make_dao


def test_key_schema_by_table_suffix():
    assert key_schema("coa-dev-session-metadata") == ("userId", "sessionId")
    assert key_schema("coa-dev-idempotency") == ("PK", None)
    assert key_schema("coa-dev-roles") == ("PK", "SK")


def test_every_registered_gsi_has_an_expression_index_in_init_sql():
    sql = " ".join(INIT_SQL.read_text(encoding="utf-8").split())
    for name, (part, sort) in GSI_REGISTRY.items():
        columns = ["table_name", f"(attributes->>'{part}') COLLATE \"C\""]
        if sort:
            columns.append(f"(attributes->>'{sort}') COLLATE \"C\"")
        assert f"ON coa_metadata ({', '.join(columns)});" in sql, name


def test_init_sql_grants_metadata_to_engine_role_only():
    sql = INIT_SQL.read_text(encoding="utf-8")
    grants = [ln for ln in sql.splitlines() if "coa_metadata" in ln and ln.startswith("GRANT")]
    assert grants == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON coa_triple, ontology_engine, coa_metadata TO coa_engine_rw;"
    ]


class TestGetPutDelete:
    def test_get_filters_expired_in_sql_and_projects(self):
        client = FakeClient(lambda sql, p: [{"a": json.dumps({"PK": "A", "SK": "B", "x": 1, "y": 2})}])
        dao = AuroraMetadataDao("coa-dev-roles", client=client)
        assert dao.get({"PK": "A", "SK": "B"}, projection=["x"]) == {"x": 1}
        sql, params = client.statements[0]
        assert "ttl IS NULL OR ttl > now()" in sql
        assert params == {"t": "coa-dev-roles", "pk": "A", "sk": "B"}

    def test_get_missing_returns_none_and_get_attribute(self):
        dao = AuroraMetadataDao("t", client=FakeClient())
        assert dao.get({"PK": "A", "SK": "B"}) is None
        assert dao.get_attribute({"PK": "A", "SK": "B"}, "x") is None

    def test_get_rejects_wrong_key_shape(self):
        dao = AuroraMetadataDao("t", client=FakeClient())
        with pytest.raises(ValueError, match="key of 't'"):
            dao.get({"PK": "A"})

    def test_unconditional_put_is_one_statement_with_ttl(self):
        client = FakeClient()
        dao = AuroraMetadataDao("t", client=client)
        dao.put({"PK": "A", "SK": "B", "ttl": 1900000000, "n": 1}, auto_timestamp=False)
        assert client.transactions == 0
        assert len(client.statements) == 1
        assert client.statements[0][1]["ttl"] == 1900000000.0

    def test_put_stamps_timestamps(self):
        client = FakeClient()
        item = {"PK": "A", "SK": "B"}
        AuroraMetadataDao("t", client=client).put(item)
        assert item["createdAt"] == item["updatedAt"]

    def test_conditional_put_fails_when_item_exists(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B"}))
        dao = AuroraMetadataDao("t", client=client)
        with pytest.raises(ClientError) as exc:
            dao.put({"PK": "A", "SK": "B"}, condition="attribute_not_exists(PK)")
        assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert client.rolled_back == 1
        assert not any(s.startswith("INSERT") for s, _ in client.statements)

    def test_conditional_put_succeeds_over_expired_item(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B"}, expired=True))
        AuroraMetadataDao("t", client=client).put({"PK": "A", "SK": "B"}, condition="attribute_not_exists(PK)")
        assert any(s.startswith("INSERT") for s, _ in client.statements)

    def test_conditional_put_takes_advisory_lock_first(self):
        client = FakeClient()
        AuroraMetadataDao("t", client=client).put({"PK": "A", "SK": "B"}, condition="attribute_not_exists(PK)")
        assert "pg_advisory_xact_lock" in client.statements[0][0]
        assert client.statements[0][1] == {"k": "t|A|B"}

    def test_delete(self):
        client = FakeClient()
        AuroraMetadataDao("t", client=client).delete({"PK": "A", "SK": "B"})
        assert client.statements[0][0].startswith("DELETE FROM coa_metadata")


class TestUpdate:
    def test_update_creates_missing_item_and_keeps_key_attributes(self):
        client = FakeClient()
        assert AuroraMetadataDao("t", client=client).update(
            {"PK": "A", "SK": "B"}, {"status": "ok"}, auto_timestamp=False
        )
        written = json.loads(client.statements[-1][1]["a"])
        assert written == {"PK": "A", "SK": "B", "status": "ok"}

    def test_update_condition_failure_raises(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B", "status": "done"}))
        dao = AuroraMetadataDao("t", client=client)
        with pytest.raises(ClientError) as exc:
            dao.update(
                {"PK": "A", "SK": "B"},
                {"status": "x"},
                condition="#s = :p",
                condition_names={"#s": "status"},
                condition_values={":p": "pending"},
            )
        assert exc.value.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def test_update_condition_failure_returns_false_when_not_raising(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B", "status": "done"}))
        ok = AuroraMetadataDao("t", client=client).update(
            {"PK": "A", "SK": "B"},
            {"status": "x"},
            condition="attribute_not_exists(PK)",
            raise_on_error=False,
        )
        assert ok is False

    def test_update_remove_fields(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B", "gone": 1, "keep": 2}))
        AuroraMetadataDao("t", client=client).update(
            {"PK": "A", "SK": "B"}, {"n": 1}, remove_fields=["gone"], auto_timestamp=False
        )
        assert json.loads(client.statements[-1][1]["a"]) == {"PK": "A", "SK": "B", "keep": 2, "n": 1}

    def test_update_placeholder_collision(self):
        dao = AuroraMetadataDao("t", client=FakeClient())
        with pytest.raises(ValueError, match="collides"):
            dao.update({"PK": "A", "SK": "B"}, {"n": 1}, condition_values={":v0": 2})

    def test_update_rewrites_ttl_column(self):
        client = FakeClient(_locked({"PK": "A", "SK": "B"}))
        AuroraMetadataDao("t", client=client).update({"PK": "A", "SK": "B"}, {"ttl": 1900000000}, auto_timestamp=False)
        assert client.statements[-1][1]["ttl"] == 1900000000.0


class TestQuery:
    def _dao(self, rows: list[dict[str, Any]]) -> tuple[AuroraMetadataDao, FakeClient]:
        client = FakeClient(lambda sql, p: rows)
        return AuroraMetadataDao("coa-dev-rrm", client=client), client

    def test_table_query_with_begins_with(self):
        dao, client = self._dao([_row({"PK": "NS#1", "SK": "ROLE#a"}, sk="ROLE#a")])
        result = dao.query(
            QueryParams(
                key_condition="#pk = :pk AND begins_with(#sk, :p)",
                expression_values={":pk": "NS#1", ":p": "ROLE#"},
                expression_names={"#pk": "PK", "#sk": "SK"},
            )
        )
        sql, params = client.statements[0]
        assert "pk = :kc_pk" in sql and "sk LIKE :kc_pfx" in sql and "ORDER BY sk ASC" in sql
        assert params["kc_pk"] == "NS#1" and params["kc_pfx"] == "ROLE#%"
        assert [i["SK"] for i in result.items] == ["ROLE#a"]
        assert result.last_evaluated_key is None

    def test_begins_with_escapes_like_wildcards(self):
        dao, client = self._dao([])
        dao.query(
            QueryParams(key_condition="PK = :pk AND begins_with(SK, :p)", expression_values={":pk": "x", ":p": "a_b%"})
        )
        assert client.statements[0][1]["kc_pfx"] == "a\\_b\\%%"

    def test_gsi_query_uses_registry_expression_and_sparse_filter(self):
        dao, client = self._dao([])
        dao.query(
            QueryParams(
                key_condition="principalKey = :pk AND begins_with(resourceRoleKey, :ns)",
                expression_values={":pk": "USER#u", ":ns": "NS#1"},
                index_name="PrincipalIndex",
            )
        )
        sql = client.statements[0][0]
        assert "(attributes->>'principalKey') COLLATE \"C\" = :kc_pk" in sql
        assert "(attributes->>'resourceRoleKey') COLLATE \"C\" LIKE :kc_pfx" in sql
        assert "(attributes->>'resourceRoleKey') COLLATE \"C\" IS NOT NULL" in sql
        assert "ORDER BY (attributes->>'resourceRoleKey') COLLATE \"C\" ASC, pk ASC, sk ASC" in sql

    def test_gsi_without_sort_attribute_orders_by_primary_key(self):
        dao, client = self._dao([])
        dao.query(
            QueryParams(
                key_condition="#n = :name",
                expression_values={":name": "x"},
                expression_names={"#n": "name"},
                index_name="NameByIndex",
            )
        )
        assert "ORDER BY pk ASC, sk ASC" in client.statements[0][0]

    def test_reverse_order_flips_direction_and_cursor_comparison(self):
        dao, client = self._dao([])
        dao.query(
            QueryParams(
                key_condition="PK = :pk",
                expression_values={":pk": "x"},
                scan_forward=False,
                exclusive_start_key={"PK": "x", "SK": "m"},
            )
        )
        sql, params = client.statements[0]
        assert "ORDER BY sk DESC" in sql and "(sk) < (:cur0)" in sql
        assert params["cur0"] == "m"

    def test_limit_returns_last_evaluated_key_only_when_more_rows_exist(self):
        rows = [_row({"PK": "x", "SK": f"{i}"}, sk=f"{i}") for i in range(3)]
        dao, client = self._dao(rows)
        page = dao.query(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}, limit=2))
        assert client.statements[0][1]["n"] == 3  # limit + 1 lookahead
        assert [i["SK"] for i in page.items] == ["0", "1"]
        assert page.last_evaluated_key == {"PK": "x", "SK": "1"}
        dao2, _ = self._dao(rows[:2])
        assert (
            dao2.query(
                QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}, limit=2)
            ).last_evaluated_key
            is None
        )

    def test_gsi_last_evaluated_key_carries_index_and_table_keys(self):
        item = {"PK": "p", "SK": "s", "principalKey": "USER#u", "resourceRoleKey": "NS#1#r"}
        rows = [_row(item, pk="p", sk="s", isk="NS#1#r"), _row({**item, "SK": "t"}, pk="p", sk="t", isk="NS#1#r")]
        dao, _ = self._dao(rows)
        page = dao.query(
            QueryParams(
                key_condition="principalKey = :pk",
                expression_values={":pk": "USER#u"},
                index_name="PrincipalIndex",
                limit=1,
            )
        )
        assert page.last_evaluated_key == {"PK": "p", "SK": "s", "principalKey": "USER#u", "resourceRoleKey": "NS#1#r"}

    def test_unknown_index_and_unsupported_parts_fail_loudly(self):
        dao, _ = self._dao([])
        with pytest.raises(NotImplementedError, match="registry"):
            dao.query(QueryParams(key_condition="a = :a", expression_values={":a": 1}, index_name="Nope"))
        with pytest.raises(NotImplementedError, match="equality condition"):
            dao.query(QueryParams(key_condition="SK = :s", expression_values={":s": "x"}))
        with pytest.raises(NotImplementedError):
            dao.query(
                QueryParams(key_condition="PK = :p AND contains(SK, :s)", expression_values={":p": "x", ":s": "y"})
            )

    def test_query_all_follows_cursor(self):
        pages = iter(
            [[_row({"PK": "x", "SK": str(i)}, sk=str(i)) for i in range(3)], [_row({"PK": "x", "SK": "2"}, sk="2")]]
        )
        client = FakeClient(lambda sql, p: next(pages))
        dao = AuroraMetadataDao("t", client=client)
        items = dao.query_all(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}, limit=2))
        assert [i["SK"] for i in items] == ["0", "1", "2"]


class TestScanAndBatches:
    def test_scan_filter_runs_in_python_and_limit_counts_evaluated(self):
        rows = [
            _row({"PK": "a", "keep": True}, sk=""),
            _row({"PK": "b", "keep": False}, sk=""),
            _row({"PK": "c", "keep": True}, sk=""),
        ]
        client = FakeClient(lambda sql, p: rows)
        page = AuroraMetadataDao("t", client=client).scan(
            filter_expression="keep = :k", expression_values={":k": True}, limit=2
        )
        assert [i["PK"] for i in page.items] == ["a"]
        assert page.last_evaluated_key == {"PK": "b", "SK": ""}

    def test_batch_get_chunks_of_100(self):
        client = FakeClient()
        AuroraMetadataDao("t", client=client).batch_get([{"PK": str(i), "SK": "x"} for i in range(250)])
        assert len(client.statements) == 3
        assert len(json.loads(client.statements[0][1]["keys"])) == 100

    def test_batch_delete_empty_is_a_noop(self):
        client = FakeClient()
        AuroraMetadataDao("t", client=client).batch_delete([])
        assert client.statements == []

    def test_atomic_increment_returns_new_value(self):
        client = FakeClient(lambda sql, p: [{"a": json.dumps({"PK": "A", "SK": "B", "n": 7})}])
        assert AuroraMetadataDao("t", client=client).atomic_increment({"PK": "A", "SK": "B"}, "n", 2) == 7
        sql, params = client.statements[0]
        assert "ON CONFLICT" in sql and json.loads(params["inc"]) == {"n": 2}

    def test_atomic_add_empty_is_a_noop(self):
        client = FakeClient()
        AuroraMetadataDao("t", client=client).atomic_add({"PK": "A", "SK": "B"}, {})
        assert client.statements == []

    def test_atomic_add_rejects_unsafe_attribute_names(self):
        with pytest.raises(ValueError, match="unsupported attribute name"):
            AuroraMetadataDao("t", client=FakeClient()).atomic_add({"PK": "A", "SK": "B"}, {"a'; DROP": 1})


class TestTransactWrite:
    def test_failed_condition_cancels_with_reasons_and_writes_nothing(self):
        client = FakeClient(_locked({"PK": "claim", "SK": "C", "namespaceId": "other"}))
        dao = AuroraMetadataDao("coa-dev-sources", client=client)
        with pytest.raises(ClientError) as exc:
            dao.transact_write(
                [
                    {
                        "Put": {
                            "TableName": "coa-dev-sources",
                            "Item": {"PK": "claim", "SK": "C", "namespaceId": "ns"},
                            "ConditionExpression": "attribute_not_exists(PK) OR namespaceId = :ns",
                            "ExpressionAttributeValues": {":ns": "ns"},
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": "coa-dev-sources",
                            "Key": {"PK": "gone", "SK": "S"},
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        assert exc.value.response["Error"]["Code"] == "TransactionCanceledException"
        assert exc.value.response["CancellationReasons"] == [
            {"Code": "ConditionalCheckFailed"},
            {"Code": "ConditionalCheckFailed"},
        ]
        assert not any(s.startswith(("INSERT", "DELETE")) for s, _ in client.statements)
        assert client.rolled_back == 1

    def test_all_conditions_pass_applies_every_operation(self):
        client = FakeClient(_locked({"PK": "src", "SK": "S", "status": "REGISTERED"}))
        # both keys read the same canned row, so use distinct tables only for the second operation
        AuroraMetadataDao("coa-dev-sources", client=client).transact_write(
            [
                {
                    "Update": {
                        "TableName": "coa-dev-sources",
                        "Key": {"PK": "src", "SK": "S"},
                        "UpdateExpression": "SET #st = :a",
                        "ConditionExpression": "#st = :r",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {":a": "APPROVED", ":r": "REGISTERED"},
                    }
                },
                {"Delete": {"TableName": "coa-dev-sources", "Key": {"PK": "old", "SK": "S"}}},
            ]
        )
        writes = [s for s, _ in client.statements if s.startswith(("INSERT", "DELETE"))]
        assert len(writes) == 2

    def test_locks_are_taken_in_sorted_key_order(self):
        client = FakeClient()
        ops = [
            {"Delete": {"TableName": "t", "Key": {"PK": "b", "SK": "1"}}},
            {"Delete": {"TableName": "t", "Key": {"PK": "a", "SK": "1"}}},
        ]
        AuroraMetadataDao("t", client=client).transact_write(ops)
        locks = [p["k"] for s, p in client.statements if "pg_advisory_xact_lock" in s]
        assert locks == ["t|a|1", "t|b|1"]

    def test_two_operations_on_one_item_are_rejected(self):
        op = {"Delete": {"TableName": "t", "Key": {"PK": "a", "SK": "1"}}}
        with pytest.raises(ClientError) as exc:
            AuroraMetadataDao("t", client=FakeClient()).transact_write([op, op])
        assert exc.value.response["Error"]["Code"] == "ValidationException"

    def test_unknown_operation(self):
        with pytest.raises(NotImplementedError):
            AuroraMetadataDao("t", client=FakeClient()).transact_write([{"Upsert": {"TableName": "t"}}])
