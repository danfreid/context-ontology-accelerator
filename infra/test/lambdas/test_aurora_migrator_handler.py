# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Aurora migrator custom-resource handler."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HANDLER_DIR = Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "aurora-migrator"
_SQL_PATH = Path(__file__).resolve().parents[2] / "lib" / "stacks" / "foundation" / "migrations" / "aurora-init.sql"
_MODULE_NAME = "aurora_migrator_index_under_test"

PROPS = {
    "ClusterArn": "arn:aws:rds:us-east-1:111122223333:cluster:c",
    "AdminSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:admin",
    "Database": "postgres",
    "Roles": [{"Name": "coa_search_rw", "SecretArn": "arn:rw"}, {"Name": "coa_search_ro", "SecretArn": "arn:ro"}],
}


@pytest.fixture
def index(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HANDLER_DIR / "index.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._rds = MagicMock()
    module._secrets = MagicMock()
    module._secrets.get_secret_value.return_value = {"SecretString": json.dumps({"password": "pw'1"})}
    module._rds.execute_statement.return_value = {"records": [[{"stringValue": "ALTER ROLE x PASSWORD 'y'"}]]}
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


def test_init_sql_splits_without_empty_statements(index):
    statements = index.split_statements(_SQL_PATH.read_text(encoding="utf-8"))
    assert statements
    assert all(s.strip() for s in statements)
    assert any("coa_search_rw" in s and s.startswith("DO $$") for s in statements)
    assert any(s.startswith("GRANT SELECT ON") or s.startswith("GRANT SELECT\n") for s in statements)


def test_create_applies_sql_then_sets_both_role_passwords(index):
    out = index.handler({"RequestType": "Create", "ResourceProperties": PROPS}, None)
    assert out["PhysicalResourceId"] == "aurora-search-migration"
    calls = index._rds.execute_statement.call_args_list
    password_calls = [c for c in calls if "parameters" in c.kwargs]
    assert [c.kwargs["parameters"][0]["value"]["stringValue"] for c in password_calls] == [
        "coa_search_rw",
        "coa_search_ro",
    ]
    # The password is bound as a parameter, never concatenated into SQL text.
    assert all("pw'1" not in c.kwargs["sql"] for c in calls)
    assert all(c.kwargs["secretArn"] == PROPS["AdminSecretArn"] for c in calls)


def test_delete_does_nothing(index):
    out = index.handler({"RequestType": "Delete", "PhysicalResourceId": "p", "ResourceProperties": PROPS}, None)
    assert out["PhysicalResourceId"] == "p"
    index._rds.execute_statement.assert_not_called()


def test_rejects_unsafe_role_name(index):
    with pytest.raises(ValueError):
        index._set_role_password(PROPS, {"Name": "x; DROP ROLE y", "SecretArn": "arn"})


def test_retries_while_cluster_resumes(index):
    from botocore.exceptions import ClientError

    resuming = ClientError(
        {"Error": {"Code": "BadRequestException", "Message": "DatabaseResumingException: resuming"}}, "ExecuteStatement"
    )
    index._rds.execute_statement.side_effect = [resuming, {"records": []}]
    assert index._execute(PROPS, "SELECT 1") == {"records": []}
    assert index._rds.execute_statement.call_count == 2
