# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""An ``rds-data`` boto client stand-in that runs the SQL on a real PostgreSQL over asyncpg.

It implements what ``coa_common.aurora_data_api.DataApiClient`` calls
(``execute_statement``, ``batch_execute_statement``, ``begin_transaction``,
``commit_transaction``, ``rollback_transaction``) with the Data API's observable
behaviour that the stores depend on:

* ``:name`` parameters are rewritten to ``$n`` (``::`` casts and quoted text are skipped);
* typed parameters (``stringValue``, ``longValue``, ...) are mapped to Python values;
* ``formatRecordsAs="JSON"`` returns ``formattedRecords`` as one JSON string;
* a transaction id pins one connection until commit or rollback;
* ``max_response_bytes`` makes a too-large result raise the service's
  ``BadRequestException`` so the paging code is exercised.

asyncpg is imported lazily so collecting this directory never needs it.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import decimal
import json
import re
import threading
import uuid
from typing import Any

from botocore.exceptions import ClientError

RESPONSE_TOO_LARGE = "Database returned more than the allowed response size limit"
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def rewrite_named_params(sql: str) -> tuple[str, list[str]]:
    """Rewrite ``:name`` to ``$n``. Returns the SQL and the ordered distinct names."""
    out: list[str] = []
    names: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j])
            i = j
        elif ch == ":" and sql.startswith("::", i):
            out.append("::")
            i += 2
        elif ch == ":" and i + 1 < n and (m := _PARAM.match(sql, i + 1)):
            name = m.group(0)
            if name not in names:
                names.append(name)
            out.append(f"${names.index(name) + 1}")
            i = m.end()
        else:
            out.append(ch)
            i += 1
    return "".join(out), names


def _param_value(typed: dict) -> Any:
    value = typed["value"]
    if value.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in value:
            return value[key]
    raise ValueError(f"unsupported parameter {typed!r}")


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _client_error(code: str, message: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


class FakeRdsDataClient:
    """boto3 ``rds-data`` look-alike over a PostgreSQL DSN."""

    def __init__(self, dsn: str, *, max_response_bytes: int | None = None):
        """Start a private event loop thread and connect a pool."""
        import asyncpg

        self._asyncpg = asyncpg
        self.max_response_bytes = max_response_bytes
        self.statements: list[str] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="fake-rds-data", daemon=True)
        self._thread.start()
        self._tx: dict[str, tuple[Any, Any]] = {}

        async def make_pool() -> Any:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=16)

        self._pool = self._run(make_pool())

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)

    def close(self) -> None:
        """Close the pool and stop the loop."""
        for conn, tx in list(self._tx.values()):
            try:
                self._run(tx.rollback())
                self._run(self._pool.release(conn))
            except Exception:
                pass
        self._run(self._pool.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # ── statements ──────────────────────────────────────────────────

    async def _execute(self, conn: Any, sql: str, params: list[dict]) -> tuple[list[dict], int]:
        text, names = rewrite_named_params(sql)
        by_name = {p["name"]: _param_value(p) for p in params}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise ValueError(f"missing parameters {missing} for statement")
        args = [by_name[n] for n in names]
        stmt = await conn.prepare(text)
        if stmt.get_attributes():
            records = await stmt.fetch(*args)
            rows = [{k: _jsonable(v) for k, v in dict(r).items()} for r in records]
            return rows, 0
        await stmt.fetch(*args)
        status = stmt.get_statusmsg() or ""
        tail = status.split()[-1] if status else "0"
        return [], int(tail) if tail.isdigit() else 0

    async def _statement(self, op: str, kwargs: dict) -> dict:
        sql = kwargs["sql"]
        self.statements.append(sql)
        params = kwargs.get("parameters") or []
        tx_id = kwargs.get("transactionId")
        try:
            if tx_id:
                if tx_id not in self._tx:
                    raise _client_error("BadRequestException", f"Transaction {tx_id} is not found", op)
                conn = self._tx[tx_id][0]
                rows, updated = await self._execute(conn, sql, params)
            else:
                async with self._pool.acquire() as conn:
                    rows, updated = await self._execute(conn, sql, params)
        except ClientError:
            raise
        except self._asyncpg.PostgresError as exc:
            raise _client_error("BadRequestException", f"{type(exc).__name__}: {exc}", op) from exc
        resp: dict[str, Any] = {"numberOfRecordsUpdated": updated}
        if kwargs.get("formatRecordsAs") == "JSON" and rows:
            formatted = json.dumps(rows)
            if self.max_response_bytes is not None and len(formatted.encode("utf-8")) > self.max_response_bytes:
                raise _client_error("BadRequestException", RESPONSE_TOO_LARGE, op)
            resp["formattedRecords"] = formatted
        return resp

    def execute_statement(self, **kwargs: Any) -> dict:
        """Run one statement."""
        return self._run(self._statement("ExecuteStatement", kwargs))

    def batch_execute_statement(self, **kwargs: Any) -> dict:
        """Run the statement once per parameter set."""
        results = []
        for ps in kwargs.get("parameterSets") or []:
            one = {k: v for k, v in kwargs.items() if k != "parameterSets"}
            one["parameters"] = ps
            self._run(self._statement("BatchExecuteStatement", one))
            results.append({"generatedFields": []})
        return {"updateResults": results}

    # ── transactions ────────────────────────────────────────────────

    async def _begin(self) -> str:
        conn = await self._pool.acquire()
        tx = conn.transaction()
        await tx.start()
        tx_id = uuid.uuid4().hex
        self._tx[tx_id] = (conn, tx)
        return tx_id

    def begin_transaction(self, **kwargs: Any) -> dict:
        """Open a transaction on a pinned connection."""
        return {"transactionId": self._run(self._begin())}

    async def _finish(self, tx_id: str, commit: bool) -> None:
        conn, tx = self._tx.pop(tx_id)
        try:
            await (tx.commit() if commit else tx.rollback())
        finally:
            await self._pool.release(conn)

    def commit_transaction(self, **kwargs: Any) -> dict:
        """Commit and release the pinned connection."""
        tx_id = kwargs["transactionId"]
        try:
            self._run(self._finish(tx_id, True))
        except self._asyncpg.PostgresError as exc:
            raise _client_error("BadRequestException", str(exc), "CommitTransaction") from exc
        return {"transactionStatus": "Transaction Committed"}

    def rollback_transaction(self, **kwargs: Any) -> dict:
        """Roll back and release the pinned connection."""
        self._run(self._finish(kwargs["transactionId"], False))
        return {"transactionStatus": "Rollback Complete"}
