# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scripted ``rds-data`` client for the Aurora unit tests (no Docker, no AWS)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError
from coa_common.aurora_data_api import AuroraConfig, AuroraDataApiClient

CONFIG = AuroraConfig(resource_arn="arn:aws:rds:r:1:cluster:c", secret_arn="arn:aws:secretsmanager:r:1:secret:s")
SCHEMA_ROW = {"has_triple": True, "has_engine": True, "has_search_doc": True}


def too_large() -> ClientError:
    """The service error for a result over 1 MiB."""
    return ClientError(
        {
            "Error": {
                "Code": "BadRequestException",
                "Message": "Database returned more than the allowed response size limit",
            }
        },
        "ExecuteStatement",
    )


class ScriptedRds:
    """Records every statement and answers from rules matched on a SQL substring (first match wins)."""

    def __init__(self, schema_row: dict | None = SCHEMA_ROW):
        """Start with the read-only schema check answered as current (tables present, no vectors stored)."""
        self.calls: list[dict[str, Any]] = []
        self.rules: list[tuple[str, Any]] = []
        self.events: list[str] = []
        if schema_row is not None:
            self.when("to_regclass", [schema_row])

    def when(self, needle: str, response: Any = None) -> None:
        """Answer statements containing ``needle`` with rows, an exception, or ``callable(sql, params)``."""
        self.rules.append((needle, response))

    def sql_calls(self, needle: str = "") -> list[dict[str, Any]]:
        """Recorded calls whose SQL contains ``needle``."""
        return [c for c in self.calls if needle in c["sql"]]

    def execute_statement(self, **kw: Any) -> dict:
        """Record and answer one statement."""
        params = {p["name"]: next(iter(p["value"].values())) for p in kw["parameters"]}
        self.calls.append({"sql": kw["sql"], "params": params, "tx": kw.get("transactionId")})
        for needle, response in self.rules:
            if needle in kw["sql"]:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    response = response(kw["sql"], params)
                    if isinstance(response, Exception):
                        raise response
                return {"formattedRecords": json.dumps(response)} if response else {"numberOfRecordsUpdated": 0}
        return {"numberOfRecordsUpdated": 0}

    def begin_transaction(self, **kw: Any) -> dict:
        """Hand out a transaction id."""
        self.events.append("begin")
        return {"transactionId": "tx-1"}

    def commit_transaction(self, **kw: Any) -> dict:
        """Record a commit."""
        self.events.append("commit")
        return {}

    def rollback_transaction(self, **kw: Any) -> dict:
        """Record a rollback."""
        self.events.append("rollback")
        return {}

    def client(self) -> AuroraDataApiClient:
        """A real ``AuroraDataApiClient`` over this script."""
        return AuroraDataApiClient(CONFIG, rds_data=self, sleep=lambda s: None)


def once_then(first: Any, then: Callable[[str, dict], Any] | list) -> Callable[[str, dict], Any]:
    """Return ``first`` on the first call and ``then`` afterwards."""
    state = {"n": 0}

    def fn(sql: str, params: dict) -> Any:
        state["n"] += 1
        if state["n"] == 1:
            return first
        return then(sql, params) if callable(then) else then

    return fn
