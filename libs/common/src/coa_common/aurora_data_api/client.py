# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin, reusable client over the boto3 ``rds-data`` service.

The Data API is HTTPS only, so a Lambda needs no VPC, endpoint or NAT gateway
to reach Aurora. It has hard limits that this client absorbs so callers do not
have to:

* **1 MiB response.** ``execute`` raises :class:`ResponseTooLargeError`;
  :meth:`AuroraDataApiClient.iter_pages` re-runs a keyset page with half the page
  size until it fits.
* **4 MiB request.** :func:`bulk_json_chunks` splits rows into JSON strings of
  at most about 1.5 MiB. The caller sends ONE string parameter per chunk and
  expands it in SQL (``jsonb_to_recordset``), which is one round trip per chunk.
  :meth:`AuroraDataApiClient.batch_execute` chunks at 500 parameter sets and 2 MiB.
* **45 s statement limit.** :class:`StatementTimeoutError` is raised and never
  retried; callers delete in bounded batches.
* **No array parameters.** Pass lists as a JSON string and expand them in SQL
  with ``jsonb_array_elements_text(CAST(:x AS jsonb))``.
* **Paused cluster.** ``DatabaseResumingException`` is retried with exponential
  backoff and jitter for ``AURORA_RESUME_TIMEOUT_SECONDS`` (default 45).

Statements are parameterised (``:name``). Parameter values, secrets and ARNs
are never logged.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import boto3
from botocore.exceptions import ClientError

from coa_common.aurora_data_api.config import AuroraConfig
from coa_common.aurora_data_api.errors import (
    AuroraAccessError,
    AuroraDataApiError,
    AuroraQueryError,
    AuroraUnavailableError,
    ResponseTooLargeError,
    ResumeTimeoutError,
    StatementTimeoutError,
)
from coa_common.aws_config import sync_boto_config

log = logging.getLogger(__name__)

MAX_BATCH_PARAMETER_SETS = 500
MAX_BATCH_BYTES = 2 * 1024 * 1024
JSON_CHUNK_BYTES = 1_500_000

_RESPONSE_TOO_LARGE = re.compile(r"response size|size limit|too large", re.IGNORECASE)
_RESUMING_MESSAGE = re.compile(r"communications link failure|resuming|is paused", re.IGNORECASE)
_RESUMING_CODES = {"DatabaseResumingException"}
_ACCESS_CODES = {"ForbiddenException", "AccessDenied", "AccessDeniedException"}
_UNAVAILABLE_CODES = {
    "ServiceUnavailableError",
    "InternalServerErrorException",
    "DatabaseUnavailableException",
    "DatabaseNotFoundException",
    "InvalidSecretException",
}

Params = Mapping[str, Any]


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _error_message(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Message", ""))


def to_data_api_param(name: str, value: Any) -> dict[str, Any]:
    """Convert one Python value to a typed Data API parameter.

    ``list`` and ``dict`` values become compact JSON text with ``typeHint`` ``JSON``; SQL reads
    them back with ``CAST(:x AS jsonb)`` (or ``CAST(:x AS vector)`` for a float list).

    Raises:
        TypeError: The value is of any other type.
    """
    if isinstance(value, list | dict):
        return {"name": name, "typeHint": "JSON", "value": {"stringValue": json.dumps(value, separators=(",", ":"))}}
    if value is None:
        field: dict[str, Any] = {"isNull": True}
    elif isinstance(value, bool):
        field = {"booleanValue": value}
    elif isinstance(value, int):
        field = {"longValue": value}
    elif isinstance(value, float):
        field = {"doubleValue": value}
    elif isinstance(value, str):
        field = {"stringValue": value}
    else:
        raise TypeError(f"Unsupported Data API parameter type for {name!r}: {type(value).__name__}")
    return {"name": name, "value": field}


def to_data_api_params(params: Params | None) -> list[dict[str, Any]]:
    """Convert a ``{name: value}`` mapping to the Data API parameter list."""
    return [to_data_api_param(k, v) for k, v in (params or {}).items()]


def _map_error(exc: ClientError) -> AuroraDataApiError:
    """Translate a botocore error into the matching :class:`AuroraDataApiError` subclass."""
    code, message = _error_code(exc), _error_message(exc)
    text = f"{code}: {message}"
    if code in _RESUMING_CODES:
        return AuroraUnavailableError(text)
    if code == "StatementTimeoutException":
        return StatementTimeoutError("Data API statement exceeded the 45 second limit")
    if code == "BadRequestException" and _RESPONSE_TOO_LARGE.search(message):
        return ResponseTooLargeError("Data API response exceeded the 1 MiB limit")
    if code in _ACCESS_CODES:
        return AuroraAccessError(text)
    if code == "BadRequestException":
        return AuroraQueryError(text)
    if code in _UNAVAILABLE_CODES:
        return AuroraUnavailableError(text)
    return AuroraDataApiError(text)


def json_text(rows: Any) -> str:
    """Compact JSON text for a list/dict parameter that SQL expands with ``CAST(:x AS jsonb)``."""
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def bulk_json_chunks(rows: Iterable[Any], max_bytes: int = JSON_CHUNK_BYTES) -> Iterator[str]:
    """Split ``rows`` into JSON array strings of at most about ``max_bytes`` UTF-8 bytes.

    A single row larger than ``max_bytes`` is emitted alone. Empty input yields nothing.
    """
    chunk: list[str] = []
    size = 2
    for row in rows:
        text = json_text(row)
        n = len(text.encode("utf-8")) + 1
        if chunk and size + n > max_bytes:
            yield "[" + ",".join(chunk) + "]"
            chunk, size = [], 2
        chunk.append(text)
        size += n
    if chunk:
        yield "[" + ",".join(chunk) + "]"


class Transaction:
    """One open Data API transaction. Obtain it from :meth:`AuroraDataApiClient.transaction`."""

    def __init__(self, client: AuroraDataApiClient, transaction_id: str):
        """Bind the transaction id to its client."""
        self._client = client
        self.transaction_id = transaction_id

    def execute(self, sql: str, params: Params | None = None) -> list[dict[str, Any]]:
        """Run a statement inside this transaction and return its rows."""
        return self._client.execute(sql, params, tx=self)

    def execute_count(self, sql: str, params: Params | None = None) -> int:
        """Run a statement inside this transaction and return ``numberOfRecordsUpdated``."""
        return self._client.execute_count(sql, params, tx=self)


class AuroraDataApiClient:
    """Executes parameterised SQL through the RDS Data API.

    ``rds_data`` (alias ``client``) is injectable for tests. When omitted, the boto3 client and the
    :class:`AuroraConfig` are created lazily on first use (``config`` may also be
    a zero-argument callable), so constructing a store never needs credentials
    or an SSM read.
    """

    def __init__(
        self,
        config: AuroraConfig | Callable[[], AuroraConfig] | None = None,
        *,
        rds_data: Any | None = None,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ):
        """Create a client; nothing is resolved until the first statement."""
        self._config_source = config
        self._config: AuroraConfig | None = config if isinstance(config, AuroraConfig) else None
        self._rds = rds_data if rds_data is not None else client
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._lock = threading.Lock()

    # ── configuration ───────────────────────────────────────────────

    @property
    def config(self) -> AuroraConfig:
        """The resolved settings (reads env/SSM on first access)."""
        if self._config is None:
            with self._lock:
                if self._config is None:
                    src = self._config_source
                    self._config = src() if callable(src) else AuroraConfig.from_env()
        return self._config

    @property
    def cluster_arn(self) -> str:
        """The cluster ARN (reads env/SSM on first access)."""
        return self.config.resource_arn

    @property
    def secret_arn(self) -> str:
        """The secret ARN (reads env/SSM on first access)."""
        return self.config.secret_arn

    def _service(self) -> Any:
        if self._rds is None:
            with self._lock:
                if self._rds is None:
                    cfg = self.config
                    self._rds = boto3.client(
                        "rds-data",
                        region_name=cfg.region,
                        config=sync_boto_config(read_timeout=60),
                    )
        return self._rds

    def _base(self) -> dict[str, Any]:
        cfg = self.config
        return {"resourceArn": cfg.resource_arn, "secretArn": cfg.secret_arn, "database": cfg.database}

    # ── retry / error mapping ───────────────────────────────────────

    def _call(self, op: str, kwargs: dict[str, Any], *, retry_resume: bool) -> dict[str, Any]:
        """Invoke one rds-data operation, mapping service errors and retrying a resuming cluster."""
        deadline = self._clock() + self.config.resume_timeout_seconds
        attempt = 0
        while True:
            try:
                return getattr(self._service(), op)(**kwargs)
            except ClientError as exc:
                code, message = _error_code(exc), _error_message(exc)
                mapped = _map_error(exc)
                if isinstance(mapped, StatementTimeoutError | ResponseTooLargeError):
                    raise mapped from exc
                resuming = code in _RESUMING_CODES or (
                    code == "BadRequestException" and _RESUMING_MESSAGE.search(message)
                )
                if not (resuming and retry_resume):
                    raise mapped from exc
                now = self._clock()
                if now >= deadline:
                    raise ResumeTimeoutError(
                        f"Aurora did not resume within {self.config.resume_timeout_seconds:g} seconds"
                    ) from exc
                delay = min(0.5 * (2**attempt), 8.0) * (0.5 + self._jitter() / 2)
                delay = min(delay, max(deadline - now, 0.0))
                attempt += 1
                log.info("Aurora is resuming; retrying in %.1fs (attempt %d)", delay, attempt)
                self._sleep(delay)

    # ── statements ──────────────────────────────────────────────────

    def _statement(self, sql: str, params: Params | None, tx: Transaction | None) -> dict[str, Any]:
        kwargs = {**self._base(), "sql": sql, "parameters": to_data_api_params(params), "formatRecordsAs": "JSON"}
        if tx is not None:
            kwargs["transactionId"] = tx.transaction_id
        return self._call("execute_statement", kwargs, retry_resume=tx is None)

    def execute(self, sql: str, params: Params | None = None, *, tx: Transaction | None = None) -> list[dict[str, Any]]:
        """Run one statement and return its rows as dicts (``[]`` when it returns none).

        Select jsonb and vector columns as ``::text`` and ``json.loads`` them: the JSON
        record format does not promise a native shape for them.
        """
        resp = self._statement(sql, params, tx)
        formatted = resp.get("formattedRecords")
        return json.loads(formatted) if formatted else []

    def execute_count(self, sql: str, params: Params | None = None, *, tx: Transaction | None = None) -> int:
        """Run one statement and return ``numberOfRecordsUpdated``."""
        return int(self._statement(sql, params, tx).get("numberOfRecordsUpdated", 0))

    def batch_execute(
        self,
        sql: str,
        param_sets: Sequence[Params],
        *,
        tx: Transaction | None = None,
    ) -> int:
        """Run ``sql`` once per parameter set in calls of at most 500 sets and 2 MiB. Returns the set count."""
        batch: list[Params] = []
        size = 0
        sent = 0

        def flush() -> int:
            kwargs = {
                **self._base(),
                "sql": sql,
                "parameterSets": [to_data_api_params(p) for p in batch],
            }
            if tx is not None:
                kwargs["transactionId"] = tx.transaction_id
            self._call("batch_execute_statement", kwargs, retry_resume=tx is None)
            return len(batch)

        for p in param_sets:
            n = len(json_text({k: v for k, v in p.items()}).encode("utf-8")) if p else 0
            if batch and (len(batch) >= MAX_BATCH_PARAMETER_SETS or size + n > MAX_BATCH_BYTES):
                sent += flush()
                batch, size = [], 0
            batch.append(p)
            size += n
        if batch:
            sent += flush()
        return sent

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Open a transaction; commit on normal exit, roll back when the block raises."""
        resp = self._call("begin_transaction", self._base(), retry_resume=True)
        tx = Transaction(self, resp["transactionId"])
        try:
            yield tx
        except BaseException:
            try:
                self._call(
                    "rollback_transaction",
                    {
                        "resourceArn": self.config.resource_arn,
                        "secretArn": self.config.secret_arn,
                        "transactionId": tx.transaction_id,
                    },
                    retry_resume=False,
                )
            except Exception:
                log.warning("Data API rollback failed", exc_info=True)
            raise
        else:
            self._call(
                "commit_transaction",
                {
                    "resourceArn": self.config.resource_arn,
                    "secretArn": self.config.secret_arn,
                    "transactionId": tx.transaction_id,
                },
                retry_resume=False,
            )

    # ── paging ──────────────────────────────────────────────────────

    def iter_pages(
        self,
        build: Callable[[Mapping[str, Any] | None, int], tuple[str, Params]],
        *,
        page_size: int = 500,
        tx: Transaction | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield keyset pages. ``build(last_row, limit)`` returns ``(sql, params)`` for the next page.

        ``last_row`` is ``None`` for the first page, else the final row of the previous
        page. When a page hits the 1 MiB response cap the page size is halved and the
        same page is read again; a single row that still does not fit re-raises
        :class:`ResponseTooLargeError`.
        """
        limit = max(1, page_size)
        last: Mapping[str, Any] | None = None
        while True:
            sql, params = build(last, limit)
            try:
                rows = self.execute(sql, params, tx=tx)
            except ResponseTooLargeError:
                if limit == 1:
                    raise
                limit = max(1, limit // 2)
                continue
            if rows:
                yield rows
            if len(rows) < limit:
                return
            last = rows[-1]

    def query_paged(
        self,
        build: Callable[[Mapping[str, Any] | None, int], tuple[str, Params]],
        *,
        page_size: int = 500,
        max_rows: int | None = None,
        tx: Transaction | None = None,
    ) -> list[dict[str, Any]]:
        """Collect :meth:`iter_pages` into one list, optionally stopping after ``max_rows``."""
        out: list[dict[str, Any]] = []
        for page in self.iter_pages(build, page_size=page_size, tx=tx):
            out.extend(page)
            if max_rows is not None and len(out) >= max_rows:
                return out[:max_rows]
        return out


DataApiClient = AuroraDataApiClient  # name used before the search layer was merged in


def build_client_from_env(env: Mapping[str, str] | None = None) -> AuroraDataApiClient:
    """Build a client from environment variables (see :class:`AuroraConfig`). Nothing is read until first use."""
    return AuroraDataApiClient(lambda: AuroraConfig.from_env(env))
