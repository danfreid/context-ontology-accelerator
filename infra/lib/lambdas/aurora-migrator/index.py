# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Custom-resource handler that applies aurora-init.sql through the RDS Data API.

Runs with the cluster admin secret and executes the idempotent DDL (extensions, search
tables, roles, grants) with continueAfterTimeout, polling pg_stat_activity until no other
DDL of the admin user is running. It then builds the HNSW index with CREATE INDEX
CONCURRENTLY, waits for pg_index.indisvalid, and drops HNSW indexes left by other
dimensions. Role passwords are read from each role's secret at runtime and passed as bound
parameters to the coa_set_role_password SQL function, so no password is ever part of SQL
text. Polling is bounded by the Lambda time left. Create and Update re-apply the SQL (the
construct passes a hash of the file, so an edit triggers an Update); Delete does nothing.

Not verified against a live Data API: the timeout and polling behaviour is covered by unit
tests with a fake client only.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_rds = boto3.client("rds-data")
_secrets = boto3.client("secretsmanager")

_SQL_PATH = Path(__file__).resolve().parent / "aurora-init.sql"
_PHYSICAL_ID = "aurora-search-migration"
_ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HNSW_INDEX_RE = re.compile(r"^search_doc_embedding_hnsw_[0-9]+$")
_TAGGED_DOLLAR_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$")
_MAX_DIMENSIONS = 2000  # HNSW on the `vector` type indexes at most 2000 dimensions

# A min-capacity-0 cluster is paused when idle; the first Data API call resumes it.
_RESUME_ATTEMPTS = 12
_RESUME_DELAY_SECONDS = 10

# Polling of long-running DDL stops this long before the Lambda deadline.
_DEADLINE_MARGIN_MS = 30_000
_DEFAULT_BUDGET_MS = 600_000  # used only when no Lambda context is passed
_POLL_SECONDS = 5


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements, honouring comments, quotes and $$ bodies.

    Tagged dollar quotes ($tag$) and E-strings are not understood by this splitter, so
    they raise ValueError instead of being split wrongly.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_dollar = False
    in_quote = False
    while i < n:
        ch = sql[i]
        if in_dollar:
            if sql.startswith("$$", i):
                in_dollar = False
                buf.append("$$")
                i += 2
                continue
            buf.append(ch)
        elif in_quote:
            buf.append(ch)
            if ch == "'":
                in_quote = False
        elif sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        elif sql.startswith("$$", i):
            in_dollar = True
            buf.append("$$")
            i += 2
            continue
        elif ch == "$" and _TAGGED_DOLLAR_RE.match(sql, i):
            raise ValueError(f"tagged dollar quote at offset {i} is not supported; use $$")
        elif ch in "Ee" and sql.startswith("'", i + 1) and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            raise ValueError(f"E-string at offset {i} is not supported; use a plain quoted string")
        elif ch == "'":
            in_quote = True
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _execute(
    props: dict[str, Any],
    sql: str,
    parameters: list[dict[str, Any]] | None = None,
    long_running: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "resourceArn": props["ClusterArn"],
        "secretArn": props["AdminSecretArn"],
        "database": props["Database"],
        "sql": sql,
    }
    if parameters:
        kwargs["parameters"] = parameters
    if long_running:
        # The Data API gives up on a call after about 45 s; with this flag the statement
        # keeps running in the database and _wait_for_ddl polls until it has finished.
        kwargs["continueAfterTimeout"] = True
    for attempt in range(_RESUME_ATTEMPTS):
        try:
            return _rds.execute_statement(**kwargs)
        except ClientError as exc:
            message = str(exc)
            resuming = "resuming" in message.lower() or "DatabaseResumingException" in message
            if not resuming or attempt == _RESUME_ATTEMPTS - 1:
                raise
            logger.info("cluster is resuming, retrying (%d)", attempt + 1)
            time.sleep(_RESUME_DELAY_SECONDS)
    raise RuntimeError("unreachable")


def _time_left_ms(context: Any) -> int:
    """Polling budget left: Lambda time remaining minus a safety margin."""
    if context is None:
        return _DEFAULT_BUDGET_MS
    return int(context.get_remaining_time_in_millis()) - _DEADLINE_MARGIN_MS


def _wait_for_ddl(props: dict[str, Any], context: Any) -> None:
    """Poll until no other session of the admin user is running CREATE/ALTER/DROP/DO."""
    while True:
        result = _execute(
            props,
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE usename = current_user AND state = 'active' AND pid <> pg_backend_pid() "
            "AND query ~* '^\\s*(CREATE|ALTER|DROP|DO)\\y'",
        )
        if int(result["records"][0][0]["longValue"]) == 0:
            return
        if _time_left_ms(context) <= 0:
            raise RuntimeError("timed out waiting for running DDL to finish")
        logger.info("waiting for running DDL to finish")
        time.sleep(_POLL_SECONDS)


def _set_role_password(props: dict[str, Any], role: dict[str, str]) -> None:
    name = role["Name"]
    if not _ROLE_NAME_RE.match(name):
        raise ValueError(f"invalid role name {name!r}")
    secret = json.loads(_secrets.get_secret_value(SecretId=role["SecretArn"])["SecretString"])
    # coa_set_role_password (aurora-init.sql) quotes the identifier and the literal on the
    # server. The password is only ever a bound parameter, never part of SQL text.
    _execute(
        props,
        "SELECT coa_set_role_password(:name, :pw)",
        [
            {"name": "name", "value": {"stringValue": name}},
            {"name": "pw", "value": {"stringValue": secret["password"]}},
        ],
    )


def _index_valid(props: dict[str, Any], name: str) -> bool | None:
    """pg_index.indisvalid of the named index, or None when the index does not exist."""
    result = _execute(
        props,
        "SELECT i.indisvalid AS valid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace",
        [{"name": "name", "value": {"stringValue": name}}],
    )
    records = result.get("records") or []
    if not records:
        return None
    return bool(records[0][0]["booleanValue"])


def _create_vector_index(props: dict[str, Any], context: Any = None) -> None:
    """Create the HNSW index as the table owner, so the rw role never needs to.

    Identifiers cannot be bound as parameters; the dimension is validated as an integer.
    The expression matches AuroraSearchIndex.ensure_vector_index and vector_search.
    The build is CONCURRENTLY (no long write lock). An invalid leftover of the same name
    (from a failed earlier build) is dropped first, and the build is awaited until
    pg_index.indisvalid is true. HNSW indexes of other dimensions are dropped afterwards.
    """
    raw = props.get("VectorDimensions")
    if raw is None:
        return
    dim = int(raw)
    if not 1 <= dim <= _MAX_DIMENSIONS:
        raise ValueError(f"VectorDimensions must be between 1 and {_MAX_DIMENSIONS}, got {dim}")
    name = f"search_doc_embedding_hnsw_{dim}"
    _wait_for_ddl(props, context)
    if _index_valid(props, name) is False:
        logger.info("dropping invalid leftover index %s", name)
        _execute(props, f"DROP INDEX IF EXISTS {name}", long_running=True)
    _execute(
        props,
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON search_doc "
        f"USING hnsw (CAST(embedding AS vector({dim})) vector_cosine_ops) "
        "WHERE embedding IS NOT NULL",
        long_running=True,
    )
    _wait_for_ddl(props, context)
    while _index_valid(props, name) is not True:
        if _time_left_ms(context) <= 0:
            raise RuntimeError(f"index {name} did not become valid before the Lambda deadline")
        logger.info("waiting for index %s to become valid", name)
        time.sleep(_POLL_SECONDS)
    _drop_stale_vector_indexes(props, name, context)


def _drop_stale_vector_indexes(props: dict[str, Any], keep: str, context: Any) -> None:
    """Drop search_doc HNSW indexes of other dimensions once the configured one is valid."""
    result = _execute(
        props,
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'search_doc'",
    )
    for record in result.get("records") or []:
        stale = record[0]["stringValue"]
        if stale == keep or not _HNSW_INDEX_RE.match(stale):
            continue
        logger.info("dropping stale index %s", stale)
        _execute(props, f"DROP INDEX IF EXISTS {stale}", long_running=True)
        _wait_for_ddl(props, context)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """CloudFormation custom-resource entry point."""
    request_type = event["RequestType"]
    logger.info("request type %s", request_type)
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or _PHYSICAL_ID}
    props = event["ResourceProperties"]
    statements = split_statements(_SQL_PATH.read_text(encoding="utf-8"))
    for statement in statements:
        _execute(props, statement, long_running=True)
        _wait_for_ddl(props, context)
    _create_vector_index(props, context)
    for role in props["Roles"]:
        _set_role_password(props, role)
    logger.info("applied %d statements and %d role passwords", len(statements), len(props["Roles"]))
    return {"PhysicalResourceId": _PHYSICAL_ID}
