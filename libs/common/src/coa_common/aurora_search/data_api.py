# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Thin RDS Data API client for the Aurora search layer.

Values are always bound as ``:name`` parameters; SQL text is never built from
values. Parameter values are never logged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from botocore.exceptions import ClientError

from coa_common.aws_config import sync_boto_config

logger = logging.getLogger(__name__)

MAX_BATCH_SETS = 500
MAX_BATCH_BYTES = 2 * 1024 * 1024
_REQUEST_OVERHEAD_BYTES = 1024
RESUME_MAX_WAIT_S = 60.0
_RESUME_MAX_DELAY_S = 16.0


class AuroraDataApiError(Exception):
    """Base class for Aurora Data API failures."""


class AuroraQueryError(AuroraDataApiError):
    """The statement was rejected (BadRequestException)."""


class AuroraTimeoutError(AuroraDataApiError):
    """The statement exceeded the Data API timeout (StatementTimeoutException)."""


class AuroraAccessError(AuroraDataApiError):
    """The caller is not allowed to use the cluster or secret."""


class AuroraResponseTooLargeError(AuroraDataApiError):
    """The result set exceeded the Data API response size limit (1 MiB)."""


class AuroraUnavailableError(AuroraDataApiError):
    """The cluster is unavailable (including a pause/resume that did not finish)."""


class _ResumingError(AuroraUnavailableError):
    """Internal: the cluster is resuming from a pause; the call is retried."""


_ACCESS_CODES = {"ForbiddenException", "AccessDenied", "AccessDeniedException"}
_UNAVAILABLE_CODES = {
    "ServiceUnavailableError",
    "InternalServerErrorException",
    "DatabaseUnavailableException",
    "DatabaseNotFoundException",
    "InvalidSecretException",
}


def _map_error(exc: ClientError) -> AuroraDataApiError:
    err = exc.response.get("Error", {})
    code = str(err.get("Code", ""))
    message = str(err.get("Message", ""))
    lowered = message.lower()
    if code == "DatabaseResumingException" or "resuming" in lowered:
        return _ResumingError(f"{code}: {message}")
    if "exceeded size limit" in lowered or "response size" in lowered:
        return AuroraResponseTooLargeError(f"{code}: {message}")
    if code == "StatementTimeoutException":
        return AuroraTimeoutError(f"{code}: {message}")
    if code in _ACCESS_CODES:
        return AuroraAccessError(f"{code}: {message}")
    if code == "BadRequestException":
        return AuroraQueryError(f"{code}: {message}")
    if code in _UNAVAILABLE_CODES:
        return AuroraUnavailableError(f"{code}: {message}")
    return AuroraDataApiError(f"{code}: {message}")


def bind_parameters(params: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convert ``{name: value}`` to Data API ``SqlParameter`` entries."""
    out: list[dict[str, Any]] = []
    for name, value in (params or {}).items():
        entry: dict[str, Any] = {"name": name}
        if value is None:
            entry["value"] = {"isNull": True}
        elif isinstance(value, bool):
            entry["value"] = {"booleanValue": value}
        elif isinstance(value, int):
            entry["value"] = {"longValue": value}
        elif isinstance(value, float):
            entry["value"] = {"doubleValue": value}
        elif isinstance(value, str):
            entry["value"] = {"stringValue": value}
        elif isinstance(value, dict | list):
            entry["value"] = {"stringValue": json.dumps(value, separators=(",", ":"))}
            entry["typeHint"] = "JSON"
        else:
            raise TypeError(f"Unsupported parameter type for {name!r}: {type(value).__name__}")
        out.append(entry)
    return out


class AuroraDataApiClient:
    """Executes SQL against an Aurora cluster through the RDS Data API."""

    def __init__(
        self,
        cluster_arn: str,
        secret_arn: str,
        database: str = "postgres",
        region: str | None = None,
        client: Any = None,
    ) -> None:
        """Create the client. Pass ``client`` to inject a fake ``rds-data`` client in tests."""
        self.cluster_arn = cluster_arn
        self.secret_arn = secret_arn
        self.database = database
        self._region = region
        self._client = client
        self._local = threading.local()
        self._client_lock = threading.Lock()

    @property
    def _tx_id(self) -> str | None:
        """Transaction id of the calling thread. Each thread has its own transaction."""
        value: str | None = getattr(self._local, "tx_id", None)
        return value

    @_tx_id.setter
    def _tx_id(self, value: str | None) -> None:
        self._local.tx_id = value

    @property
    def client(self) -> Any:
        """The ``rds-data`` boto3 client, created on first use."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import boto3

                    kwargs: dict[str, Any] = {"config": sync_boto_config(read_timeout=60)}
                    if self._region:
                        kwargs["region_name"] = self._region
                    self._client = boto3.client("rds-data", **kwargs)
        return self._client

    def _base(self) -> dict[str, Any]:
        return {"resourceArn": self.cluster_arn, "secretArn": self.secret_arn}

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Invoke a Data API method, mapping errors and retrying while the cluster resumes."""
        waited = 0.0
        delay = 1.0
        while True:
            try:
                return getattr(self.client, method)(**kwargs)
            except ClientError as exc:
                mapped = _map_error(exc)
                if not isinstance(mapped, _ResumingError):
                    raise mapped from exc
                if waited >= RESUME_MAX_WAIT_S:
                    logger.warning("Aurora cluster still resuming after %.0fs", waited)
                    raise AuroraUnavailableError(
                        f"Aurora cluster did not resume within {RESUME_MAX_WAIT_S:.0f}s"
                    ) from exc
                logger.info("Aurora cluster resuming; retrying in %.0fs", delay)
                time.sleep(delay)
                waited += delay
                delay = min(delay * 2, _RESUME_MAX_DELAY_S)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run one statement and return rows as dicts (empty for statements without a result set)."""
        kwargs: dict[str, Any] = {
            **self._base(),
            "database": self.database,
            "sql": sql,
            "formatRecordsAs": "JSON",
        }
        bound = bind_parameters(params)
        if bound:
            kwargs["parameters"] = bound
        if self._tx_id:
            kwargs["transactionId"] = self._tx_id
        response = self._call("execute_statement", **kwargs)
        formatted = response.get("formattedRecords")
        if not formatted:
            return []
        rows = json.loads(formatted)
        return rows if isinstance(rows, list) else []

    def batch_execute(self, sql: str, param_sets: list[dict[str, Any]]) -> int:
        """Run one statement for many parameter sets. Returns the number of sets sent."""
        sent = 0
        chunk: list[list[dict[str, Any]]] = []
        overhead = _REQUEST_OVERHEAD_BYTES + len(sql) + len(self.cluster_arn) + len(self.secret_arn)
        size = overhead

        def flush() -> None:
            nonlocal chunk, size, sent
            if not chunk:
                return
            kwargs: dict[str, Any] = {
                **self._base(),
                "database": self.database,
                "sql": sql,
                "parameterSets": chunk,
            }
            if self._tx_id:
                kwargs["transactionId"] = self._tx_id
            self._call("batch_execute_statement", **kwargs)
            sent += len(chunk)
            chunk = []
            size = overhead

        for params in param_sets:
            bound = bind_parameters(params)
            bound_size = len(json.dumps(bound))
            if overhead + bound_size > MAX_BATCH_BYTES:
                raise AuroraQueryError(
                    f"One parameter set is {bound_size} bytes; the request limit is {MAX_BATCH_BYTES} bytes"
                )
            if chunk and (len(chunk) >= MAX_BATCH_SETS or size + bound_size > MAX_BATCH_BYTES):
                flush()
            chunk.append(bound)
            size += bound_size
        flush()
        return sent

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the enclosed calls in one transaction. Nested use joins the outer one."""
        if self._tx_id:
            yield
            return
        response = self._call("begin_transaction", **self._base(), database=self.database)
        tx_id = response["transactionId"]
        self._tx_id = tx_id
        try:
            yield
        except BaseException:
            self._tx_id = None
            try:
                self._call(
                    "rollback_transaction", resourceArn=self.cluster_arn, secretArn=self.secret_arn, transactionId=tx_id
                )
            except AuroraDataApiError:
                logger.warning("Aurora rollback failed", exc_info=True)
            raise
        else:
            self._tx_id = None
            self._call(
                "commit_transaction", resourceArn=self.cluster_arn, secretArn=self.secret_arn, transactionId=tx_id
            )

    def iter_pages(
        self, sql: str, params: dict[str, Any] | None = None, page_size: int = 500
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield result pages for a caller-ordered statement using LIMIT/OFFSET.

        The statement must end in a total ``ORDER BY`` (unique key last), otherwise pages can
        overlap or skip rows. Halves the page size and retries when the Data API response
        limit is hit.
        """
        if "order by" not in sql.lower():
            raise ValueError("iter_pages needs a statement with a total ORDER BY")
        offset = 0
        size = max(1, page_size)
        paged_sql = f"{sql.rstrip().rstrip(';')} LIMIT :_page_limit OFFSET :_page_offset"
        while True:
            try:
                rows = self.execute(paged_sql, {**(params or {}), "_page_limit": size, "_page_offset": offset})
            except AuroraResponseTooLargeError:
                if size <= 1:
                    raise
                size = max(1, size // 2)
                logger.info("Aurora response too large; page size reduced to %d", size)
                continue
            if rows:
                yield rows
            if len(rows) < size:
                return
            offset += len(rows)
