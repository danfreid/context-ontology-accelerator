# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Custom-resource handler that applies aurora-init.sql through the RDS Data API.

Runs with the cluster admin secret, executes the idempotent DDL (extensions, search
tables, roles, grants), then sets the password of each search role from its own
Secrets Manager secret. Passwords never appear in the template: they are read at
runtime and bound as a Data API parameter. Create and Update re-apply the SQL (the
construct passes a hash of the file, so an edit triggers an Update); Delete does
nothing.
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
_MAX_DIMENSIONS = 2000  # HNSW on the `vector` type indexes at most 2000 dimensions
# A min-capacity-0 cluster is paused when idle; the first Data API call resumes it.
_RESUME_ATTEMPTS = 12
_RESUME_DELAY_SECONDS = 10


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements, honouring comments, quotes and $$ bodies."""
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


def _execute(props: dict[str, Any], sql: str, parameters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "resourceArn": props["ClusterArn"],
        "secretArn": props["AdminSecretArn"],
        "database": props["Database"],
        "sql": sql,
    }
    if parameters:
        kwargs["parameters"] = parameters
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


def _set_role_password(props: dict[str, Any], role: dict[str, str]) -> None:
    name = role["Name"]
    if not _ROLE_NAME_RE.match(name):
        raise ValueError(f"invalid role name {name!r}")
    secret = json.loads(_secrets.get_secret_value(SecretId=role["SecretArn"])["SecretString"])
    # format(%I, %L) quotes the identifier and the literal on the server; the password is
    # bound as a parameter, so it is never concatenated into SQL text here.
    result = _execute(
        props,
        "SELECT format('ALTER ROLE %I PASSWORD %L', CAST(:name AS text), CAST(:pw AS text))",
        [
            {"name": "name", "value": {"stringValue": name}},
            {"name": "pw", "value": {"stringValue": secret["password"]}},
        ],
    )
    statement = result["records"][0][0]["stringValue"]
    _execute(props, statement)


def _create_vector_index(props: dict[str, Any]) -> None:
    """Create the HNSW index as the table owner, so the rw role never needs to.

    Identifiers cannot be bound as parameters; the dimension is validated as an integer.
    The expression matches AuroraSearchIndex.ensure_vector_index and vector_search.
    """
    raw = props.get("VectorDimensions")
    if raw is None:
        return
    dim = int(raw)
    if not 1 <= dim <= _MAX_DIMENSIONS:
        raise ValueError(f"VectorDimensions must be between 1 and {_MAX_DIMENSIONS}, got {dim}")
    _execute(
        props,
        f"CREATE INDEX IF NOT EXISTS search_doc_embedding_hnsw_{dim} ON search_doc "
        f"USING hnsw (CAST(embedding AS vector({dim})) vector_cosine_ops) "
        "WHERE embedding IS NOT NULL",
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """CloudFormation custom-resource entry point."""
    request_type = event["RequestType"]
    logger.info("request type %s", request_type)
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or _PHYSICAL_ID}
    props = event["ResourceProperties"]
    statements = split_statements(_SQL_PATH.read_text(encoding="utf-8"))
    for statement in statements:
        _execute(props, statement)
    _create_vector_index(props)
    for role in props["Roles"]:
        _set_role_password(props, role)
    logger.info("applied %d statements and %d role passwords", len(statements), len(props["Roles"]))
    return {"PhysicalResourceId": _PHYSICAL_ID}
