# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the RDS Data API client (fake rds-data client, no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError
from coa_common.aurora_search import data_api
from coa_common.aurora_search.data_api import (
    AuroraAccessError,
    AuroraDataApiClient,
    AuroraQueryError,
    AuroraResponseTooLargeError,
    AuroraTimeoutError,
    AuroraUnavailableError,
    bind_parameters,
)

pytestmark = pytest.mark.unit


def client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ExecuteStatement")


class FakeRds:
    """Records calls; ``responses`` maps a method name to a queue of results or exceptions."""

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    def _call(self, method: str, kwargs: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        queue = self.responses.get(method)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return default

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        return self._call("execute_statement", kw, {"formattedRecords": "[]"})

    def batch_execute_statement(self, **kw: Any) -> dict[str, Any]:
        return self._call("batch_execute_statement", kw, {"updateResults": []})

    def begin_transaction(self, **kw: Any) -> dict[str, Any]:
        return self._call("begin_transaction", kw, {"transactionId": "tx-1"})

    def commit_transaction(self, **kw: Any) -> dict[str, Any]:
        return self._call("commit_transaction", kw, {"transactionStatus": "Transaction Committed"})

    def rollback_transaction(self, **kw: Any) -> dict[str, Any]:
        return self._call("rollback_transaction", kw, {"transactionStatus": "Rollback Complete"})

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def make(fake: FakeRds) -> AuroraDataApiClient:
    return AuroraDataApiClient("arn:cluster", "arn:secret", client=fake)


def test_bind_parameters_types() -> None:
    bound = bind_parameters({"s": "x", "i": 3, "f": 1.5, "b": True, "n": None, "d": {"a": 1}, "l": [1, 2]})
    by_name = {p["name"]: p for p in bound}
    assert by_name["s"]["value"] == {"stringValue": "x"}
    assert by_name["i"]["value"] == {"longValue": 3}
    assert by_name["f"]["value"] == {"doubleValue": 1.5}
    assert by_name["b"]["value"] == {"booleanValue": True}
    assert by_name["n"]["value"] == {"isNull": True}
    assert by_name["d"]["typeHint"] == "JSON"
    assert json.loads(by_name["d"]["value"]["stringValue"]) == {"a": 1}
    assert by_name["l"]["typeHint"] == "JSON"
    assert "typeHint" not in by_name["s"]


def test_bind_parameters_rejects_unknown_type() -> None:
    with pytest.raises(TypeError):
        bind_parameters({"x": object()})


def test_execute_parses_formatted_records() -> None:
    fake = FakeRds({"execute_statement": [{"formattedRecords": json.dumps([{"a": 1}, {"a": 2}])}]})
    rows = make(fake).execute("SELECT a FROM t WHERE x = :x", {"x": "v"})
    assert rows == [{"a": 1}, {"a": 2}]
    _, kw = fake.calls[0]
    assert kw["formatRecordsAs"] == "JSON"
    assert kw["resourceArn"] == "arn:cluster"
    assert kw["secretArn"] == "arn:secret"
    assert kw["database"] == "postgres"
    assert kw["parameters"][0]["name"] == "x"
    assert "transactionId" not in kw


def test_execute_without_result_set_returns_empty() -> None:
    assert make(FakeRds({"execute_statement": [{}]})).execute("CREATE INDEX x") == []


def test_batch_execute_chunks_at_500_sets() -> None:
    fake = FakeRds()
    sent = make(fake).batch_execute("INSERT INTO t VALUES (:a)", [{"a": i} for i in range(1201)])
    sizes = [len(kw["parameterSets"]) for m, kw in fake.calls if m == "batch_execute_statement"]
    assert sizes == [500, 500, 201]
    assert sent == 1201


def test_batch_execute_chunks_by_size() -> None:
    fake = FakeRds()
    big = "x" * (600 * 1024)
    make(fake).batch_execute("INSERT INTO t VALUES (:a)", [{"a": big} for _ in range(5)])
    sizes = [len(kw["parameterSets"]) for _, kw in fake.calls]
    assert sum(sizes) == 5
    assert max(sizes) <= 3
    for _, kw in fake.calls:
        assert len(json.dumps(kw["parameterSets"])) <= data_api.MAX_BATCH_BYTES + 1024


def test_batch_execute_rejects_single_oversized_parameter_set() -> None:
    fake = FakeRds()
    huge = "x" * data_api.MAX_BATCH_BYTES
    with pytest.raises(AuroraQueryError, match="parameter set"):
        make(fake).batch_execute("INSERT INTO t VALUES (:a)", [{"a": "ok"}, {"a": huge}])
    assert fake.calls == []


def test_batch_execute_counts_request_overhead() -> None:
    fake = FakeRds()
    almost = "x" * (data_api.MAX_BATCH_BYTES - 200)  # fits alone only if overhead is ignored
    with pytest.raises(AuroraQueryError):
        make(fake).batch_execute("INSERT INTO t VALUES (:a)", [{"a": almost}])


def test_iter_pages_requires_order_by() -> None:
    with pytest.raises(ValueError, match="ORDER BY"):
        list(make(FakeRds()).iter_pages("SELECT i FROM t"))


def test_each_thread_has_its_own_transaction_id() -> None:
    import threading

    class TxRds(FakeRds):
        def __init__(self) -> None:
            super().__init__()
            self.counter = 0
            self.lock = threading.Lock()

        def begin_transaction(self, **kw: Any) -> dict[str, Any]:
            with self.lock:
                self.counter += 1
                return {"transactionId": f"tx-{self.counter}"}

    fake = TxRds()
    c = make(fake)
    both_open = threading.Barrier(2, timeout=10)
    seen: dict[str, str | None] = {}

    def worker(name: str) -> None:
        with c.transaction():
            both_open.wait()  # both threads are inside their transaction at the same time
            c.execute(f"SELECT '{name}'")
            seen[name] = c._tx_id
        seen[f"{name}-after"] = c._tx_id

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert {seen["a"], seen["b"]} == {"tx-1", "tx-2"}
    assert seen["a-after"] is None and seen["b-after"] is None
    used = {kw["sql"]: kw["transactionId"] for m, kw in fake.calls if m == "execute_statement"}
    assert used["SELECT 'a'"] == seen["a"]
    assert used["SELECT 'b'"] == seen["b"]
    assert c._tx_id is None  # the main thread never joined either transaction


def test_batch_execute_empty_makes_no_call() -> None:
    fake = FakeRds()
    assert make(fake).batch_execute("INSERT INTO t VALUES (:a)", []) == 0
    assert fake.calls == []


def test_transaction_commit_passes_transaction_id() -> None:
    fake = FakeRds()
    c = make(fake)
    with c.transaction():
        c.execute("SELECT 1")
        c.batch_execute("INSERT INTO t VALUES (:a)", [{"a": 1}])
    assert fake.methods() == ["begin_transaction", "execute_statement", "batch_execute_statement", "commit_transaction"]
    assert fake.calls[1][1]["transactionId"] == "tx-1"
    assert fake.calls[2][1]["transactionId"] == "tx-1"
    assert fake.calls[3][1]["transactionId"] == "tx-1"
    c.execute("SELECT 2")
    assert "transactionId" not in fake.calls[-1][1]


def test_transaction_rollback_on_error() -> None:
    fake = FakeRds()
    c = make(fake)
    with pytest.raises(RuntimeError), c.transaction():
        c.execute("SELECT 1")
        raise RuntimeError("fail")
    assert fake.methods() == ["begin_transaction", "execute_statement", "rollback_transaction"]
    assert "commit_transaction" not in fake.methods()


def test_nested_transaction_joins_outer() -> None:
    fake = FakeRds()
    c = make(fake)
    with c.transaction(), c.transaction():
        c.execute("SELECT 1")
    assert fake.methods().count("begin_transaction") == 1
    assert fake.methods().count("commit_transaction") == 1


def test_iter_pages_pages_until_short_page() -> None:
    pages = [
        {"formattedRecords": json.dumps([{"i": 0}, {"i": 1}])},
        {"formattedRecords": json.dumps([{"i": 2}])},
    ]
    fake = FakeRds({"execute_statement": pages})
    out = list(make(fake).iter_pages("SELECT i FROM t ORDER BY i", {"x": 1}, page_size=2))
    assert out == [[{"i": 0}, {"i": 1}], [{"i": 2}]]
    sql = fake.calls[0][1]["sql"]
    assert sql.endswith("LIMIT :_page_limit OFFSET :_page_offset")
    offsets = [
        next(p for p in kw["parameters"] if p["name"] == "_page_offset")["value"]["longValue"] for _, kw in fake.calls
    ]
    assert offsets == [0, 2]


def test_iter_pages_halves_on_response_limit() -> None:
    too_large = client_error("BadRequestException", "Database response exceeded size limit of 1,048,575 bytes")
    fake = FakeRds(
        {
            "execute_statement": [
                too_large,
                too_large,
                {"formattedRecords": json.dumps([{"i": 0}])},
            ]
        }
    )
    out = list(make(fake).iter_pages("SELECT i FROM t ORDER BY i", page_size=4))
    assert out == [[{"i": 0}]]
    limits = [
        next(p for p in kw["parameters"] if p["name"] == "_page_limit")["value"]["longValue"] for _, kw in fake.calls
    ]
    assert limits == [4, 2, 1, 1]  # the full page of 1 row triggers one more fetch


def test_iter_pages_raises_when_single_row_too_large() -> None:
    too_large = client_error("BadRequestException", "Database response exceeded size limit")
    fake = FakeRds({"execute_statement": [too_large] * 5})
    with pytest.raises(AuroraResponseTooLargeError):
        list(make(fake).iter_pages("SELECT i FROM t ORDER BY i", page_size=1))


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("BadRequestException", "syntax error", AuroraQueryError),
        ("StatementTimeoutException", "timed out", AuroraTimeoutError),
        ("ForbiddenException", "denied", AuroraAccessError),
        ("AccessDenied", "denied", AuroraAccessError),
        ("BadRequestException", "Database response exceeded size limit", AuroraResponseTooLargeError),
        ("ServiceUnavailableError", "down", AuroraUnavailableError),
        ("InternalServerErrorException", "oops", AuroraUnavailableError),
    ],
)
def test_error_mapping(code: str, message: str, expected: type[Exception]) -> None:
    fake = FakeRds({"execute_statement": [client_error(code, message)]})
    with pytest.raises(expected):
        make(fake).execute("SELECT 1")


def test_resuming_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(data_api.time, "sleep", sleeps.append)
    resuming = client_error("DatabaseResumingException", "The Aurora DB instance is resuming after being auto-paused")
    fake = FakeRds({"execute_statement": [resuming, resuming, {"formattedRecords": json.dumps([{"ok": 1}])}]})
    assert make(fake).execute("SELECT 1") == [{"ok": 1}]
    assert sleeps == [1.0, 2.0]


def test_resuming_message_without_code_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_api.time, "sleep", lambda _s: None)
    resuming = client_error("BadRequestException", "Communications link failure: cluster is resuming")
    fake = FakeRds({"execute_statement": [resuming]})
    assert make(fake).execute("SELECT 1") == []


def test_resuming_exhaustion_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(data_api.time, "sleep", sleeps.append)
    resuming = client_error("DatabaseResumingException", "resuming")
    fake = FakeRds({"execute_statement": [resuming] * 50})
    with pytest.raises(AuroraUnavailableError):
        make(fake).execute("SELECT 1")
    assert 60 <= sum(sleeps) <= 80
    assert len(fake.calls) < 50


def test_boto_client_built_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    def fake_client(service: str, **kw: Any) -> object:
        created.append({"service": service, **kw})
        return object()

    import boto3

    monkeypatch.setattr(boto3, "client", fake_client)
    c = AuroraDataApiClient("arn:c", "arn:s", region="us-west-2")
    assert created == []
    _ = c.client
    assert created[0]["service"] == "rds-data"
    assert created[0]["region_name"] == "us-west-2"
    assert created[0]["config"] is not None
