# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Aurora migrator custom-resource handler."""

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_HANDLER_DIR = Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "aurora-migrator"
_SQL_PATH = Path(__file__).resolve().parents[2] / "lib" / "stacks" / "foundation" / "migrations" / "aurora-init.sql"
_MODULE_NAME = "aurora_migrator_index_under_test"

PASSWORD = "pw'1"
PASSWORD_STATEMENT = "SELECT coa_set_role_password(:name, :pw)"
PROPS = {
    "ClusterArn": "arn:aws:rds:us-east-1:111122223333:cluster:c",
    "AdminSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:admin",
    "Database": "postgres",
    "Roles": [{"Name": "coa_search_rw", "SecretArn": "arn:rw"}, {"Name": "coa_search_ro", "SecretArn": "arn:ro"}],
}


class FakeContext:
    """Lambda context whose remaining time shrinks by 'step' ms on every call."""

    def __init__(self, remaining_ms: int = 900_000, step: int = 0) -> None:
        self.remaining_ms = remaining_ms
        self.step = step
        self.calls = 0

    def get_remaining_time_in_millis(self) -> int:
        self.calls += 1
        value = self.remaining_ms
        self.remaining_ms -= self.step
        return value


class FakeRds:
    """Fake rds-data client. 'active' and 'valid' are consumed one entry per poll (last one repeats)."""

    def __init__(self, active: list[int] | None = None, valid: list[bool | None] | None = None) -> None:
        self.active = list(active or [0])
        self.valid = list(valid or [True])
        self.stale: list[str] = []
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _pop(values: list[Any]) -> Any:
        return values.pop(0) if len(values) > 1 else values[0]

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        sql = kwargs["sql"]
        if "FROM pg_stat_activity" in sql:
            return {"records": [[{"longValue": self._pop(self.active)}]]}
        if "indisvalid" in sql:
            state = self._pop(self.valid)
            return {"records": [] if state is None else [[{"booleanValue": state}]]}
        if "FROM pg_indexes" in sql:
            return {"records": [[{"stringValue": n}] for n in self.stale]}
        return {"records": []}

    def sqls(self) -> list[str]:
        return [c["sql"] for c in self.calls]


@pytest.fixture
def index(monkeypatch):
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HANDLER_DIR / "index.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._rds = FakeRds()
    module._secrets = MagicMock()
    module._secrets.get_secret_value.return_value = {"SecretString": json.dumps({"password": PASSWORD})}
    module._SQL_PATH = _SQL_PATH
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    return module


def test_split_keeps_dollar_bodies_and_drops_top_level_comments(index):
    sql = "-- a; comment\nCREATE TABLE t (a INT);\nDO $$\nBEGIN\n  PERFORM 1; -- x\nEND\n$$;\nSELECT 'a;b';"
    assert index.split_statements(sql) == [
        "CREATE TABLE t (a INT)",
        "DO $$\nBEGIN\n  PERFORM 1; -- x\nEND\n$$",
        "SELECT 'a;b'",
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "DO $body$ BEGIN PERFORM 1; END $body$;",
        "SELECT E'a\\'b';",
        "SELECT e'x';",
        "SELECT 1; SELECT $_t$x$_t$;",
    ],
)
def test_split_rejects_tagged_dollar_quotes_and_e_strings(index, sql):
    with pytest.raises(ValueError):
        index.split_statements(sql)


def test_split_accepts_lookalikes(index):
    sql = "SELECT 'E''x'; -- E'ignored in a comment\nSELECT type'a', $1, price$2;"
    assert index.split_statements(sql) == ["SELECT 'E''x'", "SELECT type'a', $1, price$2"]


def test_init_sql_splits_without_empty_statements(index):
    statements = index.split_statements(_SQL_PATH.read_text(encoding="utf-8"))
    assert statements
    assert all(s.strip() for s in statements)
    assert any("coa_search_rw" in s and s.startswith("DO $$") for s in statements)
    assert any(s.startswith("GRANT SELECT ON") or s.startswith("GRANT SELECT\n") for s in statements)
    assert any(s.startswith("CREATE OR REPLACE FUNCTION coa_set_role_password") for s in statements)


def test_create_sets_passwords_through_function_with_bound_parameters_only(index):
    ctx = FakeContext()
    out = index.handler({"RequestType": "Create", "ResourceProperties": PROPS}, ctx)
    assert out["PhysicalResourceId"] == "aurora-search-migration"
    calls = index._rds.calls
    password_calls = [c for c in calls if c["sql"] == PASSWORD_STATEMENT]
    assert [c["parameters"] for c in password_calls] == [
        [
            {"name": "name", "value": {"stringValue": "coa_search_rw"}},
            {"name": "pw", "value": {"stringValue": PASSWORD}},
        ],
        [
            {"name": "name", "value": {"stringValue": "coa_search_ro"}},
            {"name": "pw", "value": {"stringValue": PASSWORD}},
        ],
    ]
    # The password appears in the parameters list only, never in any SQL text.
    assert all(PASSWORD not in c["sql"] for c in calls)
    others = [c for c in calls if c["sql"] != PASSWORD_STATEMENT]
    assert all(PASSWORD not in json.dumps(c.get("parameters", [])) for c in others)
    assert all("ALTER ROLE" not in c["sql"] or c["sql"].startswith("CREATE OR REPLACE FUNCTION") for c in calls)
    assert all(c["secretArn"] == PROPS["AdminSecretArn"] for c in calls)


def test_ddl_statements_use_continue_after_timeout_and_password_calls_do_not(index):
    index.handler({"RequestType": "Create", "ResourceProperties": {**PROPS, "VectorDimensions": 768}}, FakeContext())
    ddl = [
        c
        for c in index._rds.calls
        if c["sql"].lstrip().startswith(("CREATE", "ALTER", "DROP", "DO", "GRANT", "REVOKE"))
    ]
    assert ddl
    assert all(c.get("continueAfterTimeout") is True for c in ddl)
    reads = [c for c in index._rds.calls if c["sql"].lstrip().startswith("SELECT")]
    assert reads
    assert all("continueAfterTimeout" not in c for c in reads)


def test_each_ddl_statement_is_followed_by_a_wait_for_running_ddl(index):
    index.handler({"RequestType": "Create", "ResourceProperties": PROPS}, FakeContext())
    sqls = index._rds.sqls()
    for pos, sql in enumerate(sqls):
        if sql.lstrip().startswith(("CREATE", "ALTER", "DO", "GRANT", "REVOKE")):
            assert "pg_stat_activity" in sqls[pos + 1]


def test_wait_for_ddl_polls_until_no_other_ddl_is_active(index):
    index._rds = FakeRds(active=[2, 1, 0])
    index._wait_for_ddl(PROPS, FakeContext())
    polls = [s for s in index._rds.sqls() if "pg_stat_activity" in s]
    assert len(polls) == 3
    assert "pid <> pg_backend_pid()" in polls[0]
    assert "usename = current_user" in polls[0]
    assert "state = 'active'" in polls[0]
    assert "CREATE|ALTER|DROP|DO" in polls[0]


def test_wait_for_ddl_is_bounded_by_the_lambda_deadline(index):
    index._rds = FakeRds(active=[1])
    ctx = FakeContext(remaining_ms=100_000, step=40_000)  # 100k, 60k, 20k (< 30k margin)
    with pytest.raises(RuntimeError, match="DDL"):
        index._wait_for_ddl(PROPS, ctx)
    assert ctx.calls == 3


def test_delete_does_nothing(index):
    out = index.handler(
        {"RequestType": "Delete", "PhysicalResourceId": "p", "ResourceProperties": PROPS}, FakeContext()
    )
    assert out["PhysicalResourceId"] == "p"
    assert index._rds.calls == []


def test_rejects_unsafe_role_name(index):
    with pytest.raises(ValueError):
        index._set_role_password(PROPS, {"Name": "x; DROP ROLE y", "SecretArn": "arn"})


def test_retries_while_cluster_resumes(index):
    from botocore.exceptions import ClientError

    resuming = ClientError(
        {"Error": {"Code": "BadRequestException", "Message": "DatabaseResumingException: resuming"}}, "ExecuteStatement"
    )
    index._rds = MagicMock()
    index._rds.execute_statement.side_effect = [resuming, {"records": []}]
    assert index._execute(PROPS, "SELECT 1") == {"records": []}
    assert index._rds.execute_statement.call_count == 2


def test_creates_hnsw_index_concurrently_as_admin_for_configured_dimension(index):
    index.handler({"RequestType": "Create", "ResourceProperties": {**PROPS, "VectorDimensions": 768}}, FakeContext())
    creates = [
        c for c in index._rds.calls if "search_doc_embedding_hnsw_768" in c["sql"] and "CREATE INDEX" in c["sql"]
    ]
    assert len(creates) == 1
    assert creates[0]["sql"].startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS search_doc_embedding_hnsw_768")
    assert "vector(768)" in creates[0]["sql"]
    assert creates[0]["continueAfterTimeout"] is True


def test_hnsw_build_waits_until_index_is_valid(index):
    index._rds = FakeRds(valid=[None, False, False, True])
    index._create_vector_index({**PROPS, "VectorDimensions": 3}, FakeContext())
    validity_checks = [c for c in index._rds.calls if "indisvalid" in c["sql"]]
    # leftover probe (missing), then polls: not valid, not valid, valid
    assert len(validity_checks) == 4
    assert validity_checks[0]["parameters"] == [
        {"name": "name", "value": {"stringValue": "search_doc_embedding_hnsw_3"}}
    ]
    assert not any("DROP INDEX" in s for s in index._rds.sqls())


def test_hnsw_build_gives_up_at_the_lambda_deadline(index):
    index._rds = FakeRds(valid=[None, False])
    ctx = FakeContext(remaining_ms=100_000, step=40_000)
    with pytest.raises(RuntimeError, match="valid"):
        index._create_vector_index({**PROPS, "VectorDimensions": 3}, ctx)


def test_invalid_leftover_index_is_dropped_before_the_build(index):
    index._rds = FakeRds(valid=[False, True])
    index._create_vector_index({**PROPS, "VectorDimensions": 3}, FakeContext())
    sqls = index._rds.sqls()
    drop = next(i for i, s in enumerate(sqls) if s == "DROP INDEX IF EXISTS search_doc_embedding_hnsw_3")
    create = next(i for i, s in enumerate(sqls) if s.startswith("CREATE INDEX CONCURRENTLY"))
    assert drop < create
    assert next(c for c in index._rds.calls if c["sql"].startswith("DROP INDEX"))["continueAfterTimeout"] is True


def test_stale_dimension_indexes_are_dropped_after_the_new_one_is_valid(index):
    index._rds = FakeRds(valid=[None, True])
    index._rds.stale = [
        "search_doc_embedding_hnsw_3",
        "search_doc_embedding_hnsw_768",
        "search_doc_embedding_hnsw_1536",
        "search_doc_embedding_hnsw_3; DROP TABLE search_doc",
        "search_doc_tsv_idx",
    ]
    index._create_vector_index({**PROPS, "VectorDimensions": 3}, FakeContext())
    sqls = index._rds.sqls()
    drops = [s for s in sqls if s.startswith("DROP INDEX")]
    assert drops == [
        "DROP INDEX IF EXISTS search_doc_embedding_hnsw_768",
        "DROP INDEX IF EXISTS search_doc_embedding_hnsw_1536",
    ]
    create = next(i for i, s in enumerate(sqls) if s.startswith("CREATE INDEX CONCURRENTLY"))
    assert all(sqls.index(d) > create for d in drops)
    assert all(c["continueAfterTimeout"] for c in index._rds.calls if c["sql"].startswith("DROP INDEX"))


def test_no_vector_index_work_without_dimensions(index):
    index.handler({"RequestType": "Create", "ResourceProperties": PROPS}, FakeContext())
    assert not any("hnsw" in s for s in index._rds.sqls() if "search_doc_embedding" in s)
    assert not any(s.startswith("CREATE INDEX CONCURRENTLY") for s in index._rds.sqls())


def test_rejects_out_of_range_vector_dimensions(index):
    with pytest.raises(ValueError):
        index.handler(
            {"RequestType": "Create", "ResourceProperties": {**PROPS, "VectorDimensions": 4000}}, FakeContext()
        )


def test_handler_passes_context_to_the_polling_helpers(index):
    ctx = FakeContext()
    index.handler({"RequestType": "Create", "ResourceProperties": {**PROPS, "VectorDimensions": 3}}, ctx)
    index._rds = FakeRds(active=[1, 0])
    seen: list[Any] = []
    original = index._time_left_ms
    index._time_left_ms = lambda c: (seen.append(c), original(c))[1]
    index.handler({"RequestType": "Create", "ResourceProperties": PROPS}, ctx)
    assert seen and all(c is ctx for c in seen)
