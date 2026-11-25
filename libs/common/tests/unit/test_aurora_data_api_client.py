# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Aurora Data API client (no AWS calls)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_common.aurora_data_api import (
    AuroraConfig,
    DataApiClient,
    ResponseTooLargeError,
    ResumeTimeoutError,
    StatementTimeoutError,
    bulk_json_chunks,
    to_data_api_params,
)

pytestmark = pytest.mark.unit

CFG = AuroraConfig(
    resource_arn="arn:aws:rds:r:1:cluster:c", secret_arn="arn:aws:secretsmanager:r:1:secret:s", database="db"
)


def _err(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ExecuteStatement")


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(rds: MagicMock, clock: _Clock | None = None, cfg: AuroraConfig = CFG) -> DataApiClient:
    clock = clock or _Clock()
    return DataApiClient(cfg, rds_data=rds, sleep=clock.sleep, clock=clock.time, jitter=lambda: 0.0)


def test_typed_parameters():
    params = to_data_api_params({"a": "x", "b": 3, "c": 1.5, "d": True, "e": None})
    by_name = {p["name"]: p["value"] for p in params}
    assert by_name == {
        "a": {"stringValue": "x"},
        "b": {"longValue": 3},
        "c": {"doubleValue": 1.5},
        "d": {"booleanValue": True},
        "e": {"isNull": True},
    }
    with pytest.raises(TypeError):
        to_data_api_params({"x": [1]})


def test_execute_sends_json_format_and_parses_rows():
    rds = MagicMock()
    rds.execute_statement.return_value = {"formattedRecords": json.dumps([{"n": 1}])}
    rows = _client(rds).execute("SELECT :a AS n", {"a": 1})
    assert rows == [{"n": 1}]
    kwargs = rds.execute_statement.call_args.kwargs
    assert kwargs["formatRecordsAs"] == "JSON"
    assert kwargs["sql"] == "SELECT :a AS n"
    assert kwargs["database"] == "db"
    assert kwargs["parameters"] == [{"name": "a", "value": {"longValue": 1}}]


def test_execute_without_records_returns_empty_list():
    rds = MagicMock()
    rds.execute_statement.return_value = {"numberOfRecordsUpdated": 3}
    c = _client(rds)
    assert c.execute("DELETE FROM t") == []
    assert c.execute_count("DELETE FROM t") == 3


def test_resume_retry_then_success_uses_backoff():
    rds = MagicMock()
    rds.execute_statement.side_effect = [
        _err("DatabaseResumingException"),
        _err("DatabaseResumingException"),
        {"formattedRecords": "[]"},
    ]
    clock = _Clock()
    assert _client(rds, clock).execute("SELECT 1") == []
    assert rds.execute_statement.call_count == 3
    assert clock.sleeps == [0.25, 0.5]


def test_communications_link_failure_is_treated_as_resuming():
    rds = MagicMock()
    rds.execute_statement.side_effect = [
        _err("BadRequestException", "Communications link failure"),
        {"formattedRecords": "[]"},
    ]
    _client(rds).execute("SELECT 1")
    assert rds.execute_statement.call_count == 2


def test_resume_timeout():
    rds = MagicMock()
    rds.execute_statement.side_effect = _err("DatabaseResumingException")
    cfg = AuroraConfig(resource_arn="a", secret_arn="s", resume_timeout_seconds=3)
    with pytest.raises(ResumeTimeoutError):
        _client(rds, cfg=cfg).execute("SELECT 1")
    assert rds.execute_statement.call_count > 1


def test_statement_timeout_is_not_retried():
    rds = MagicMock()
    rds.execute_statement.side_effect = _err("StatementTimeoutException")
    with pytest.raises(StatementTimeoutError):
        _client(rds).execute("SELECT pg_sleep(60)")
    assert rds.execute_statement.call_count == 1


def test_other_errors_propagate_unchanged():
    rds = MagicMock()
    rds.execute_statement.side_effect = _err("BadRequestException", 'relation "x" does not exist')
    with pytest.raises(ClientError):
        _client(rds).execute("SELECT * FROM x")
    assert rds.execute_statement.call_count == 1


def test_response_too_large_mapped():
    rds = MagicMock()
    rds.execute_statement.side_effect = _err(
        "BadRequestException", "Database returned more than the allowed response size limit"
    )
    with pytest.raises(ResponseTooLargeError):
        _client(rds).execute("SELECT * FROM big")


def test_iter_pages_halves_page_on_response_limit_and_keysets():
    limits: list[int] = []

    def execute_statement(**kwargs):
        params = {p["name"]: p["value"] for p in kwargs["parameters"]}
        limit = params["limit"]["longValue"]
        after = params["after"]["longValue"]
        limits.append(limit)
        if limit > 2:
            raise _err("BadRequestException", "Database returned more than the allowed response size limit")
        rows = [{"id": i} for i in range(after + 1, min(after + limit, 5) + 1)]
        return {"formattedRecords": json.dumps(rows)}

    rds = MagicMock()
    rds.execute_statement.side_effect = execute_statement

    def build(last, limit):
        return "SELECT id FROM t WHERE id > :after ORDER BY id LIMIT :limit", {
            "after": last["id"] if last else 0,
            "limit": limit,
        }

    rows = _client(rds).query_paged(build, page_size=8)
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
    assert limits[:3] == [8, 4, 2]


def test_iter_pages_single_oversized_row_raises():
    rds = MagicMock()
    rds.execute_statement.side_effect = _err("BadRequestException", "response size limit")
    with pytest.raises(ResponseTooLargeError):
        _client(rds).query_paged(lambda last, n: ("SELECT 1", {}), page_size=2)


def test_query_paged_max_rows():
    rds = MagicMock()
    rds.execute_statement.return_value = {"formattedRecords": json.dumps([{"id": 1}, {"id": 2}])}
    rows = _client(rds).query_paged(lambda last, n: ("SELECT 1", {}), page_size=2, max_rows=3)
    assert len(rows) == 3


def test_bulk_json_chunks_respect_size_and_keep_every_row():
    rows = [{"k": i, "v": "x" * 100} for i in range(100)]
    chunks = list(bulk_json_chunks(rows, max_bytes=2000))
    assert len(chunks) > 1
    assert all(len(c.encode()) <= 2000 + 130 for c in chunks)
    assert [r["k"] for c in chunks for r in json.loads(c)] == list(range(100))
    assert list(bulk_json_chunks([])) == []


def test_bulk_json_chunk_oversize_row_is_emitted_alone():
    chunks = list(bulk_json_chunks([{"v": "x" * 500}, {"v": "y"}], max_bytes=100))
    assert len(chunks) == 2


def test_batch_execute_chunks_at_500_sets():
    rds = MagicMock()
    rds.batch_execute_statement.return_value = {}
    n = _client(rds).batch_execute("INSERT INTO t VALUES (:a)", [{"a": i} for i in range(1100)])
    assert n == 1100
    sizes = [len(c.kwargs["parameterSets"]) for c in rds.batch_execute_statement.call_args_list]
    assert sizes == [500, 500, 100]


def test_batch_execute_chunks_by_bytes():
    rds = MagicMock()
    rds.batch_execute_statement.return_value = {}
    big = "x" * 900_000
    _client(rds).batch_execute("INSERT INTO t VALUES (:a)", [{"a": big}] * 5)
    sizes = [len(c.kwargs["parameterSets"]) for c in rds.batch_execute_statement.call_args_list]
    assert sum(sizes) == 5
    assert max(sizes) <= 2


def test_transaction_commits_and_passes_id():
    rds = MagicMock()
    rds.begin_transaction.return_value = {"transactionId": "tx-1"}
    rds.execute_statement.return_value = {"formattedRecords": "[]"}
    with _client(rds).transaction() as tx:
        tx.execute("SELECT 1")
    assert rds.execute_statement.call_args.kwargs["transactionId"] == "tx-1"
    rds.commit_transaction.assert_called_once()
    rds.rollback_transaction.assert_not_called()


def test_transaction_rolls_back_on_error_and_reraises():
    rds = MagicMock()
    rds.begin_transaction.return_value = {"transactionId": "tx-2"}
    with pytest.raises(RuntimeError, match="nope"), _client(rds).transaction():
        raise RuntimeError("nope")
    rds.rollback_transaction.assert_called_once()
    rds.commit_transaction.assert_not_called()


def test_statements_in_a_transaction_are_not_retried_on_resume():
    rds = MagicMock()
    rds.begin_transaction.return_value = {"transactionId": "tx-3"}
    rds.execute_statement.side_effect = _err("DatabaseResumingException")
    with pytest.raises(ClientError), _client(rds).transaction() as tx:
        tx.execute("SELECT 1")
    assert rds.execute_statement.call_count == 1
    rds.rollback_transaction.assert_called_once()


def test_sql_and_arns_are_not_logged(caplog):
    rds = MagicMock()
    rds.execute_statement.return_value = {"formattedRecords": "[]"}
    with caplog.at_level("DEBUG"):
        _client(rds).execute("SELECT :secret_value", {"secret_value": "hunter2"})
    text = caplog.text
    assert "hunter2" not in text
    assert CFG.resource_arn not in text
    assert CFG.secret_arn not in text
