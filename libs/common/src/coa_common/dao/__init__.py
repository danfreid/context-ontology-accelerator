# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data Access Object pattern: ABC, DynamoDB implementation, Aurora implementation and the backend switch."""

import os
from collections.abc import Callable
from typing import Any

from botocore.config import Config

from coa_common.dao.base import DatastoreDAO, PaginatedResult, QueryParams
from coa_common.dao.dynamodb import DynamoDBDAO

METADATA_BACKEND_ENV = "METADATA_BACKEND"


def aurora_metadata_enabled() -> bool:
    """True when ``METADATA_BACKEND=aurora``. Callers that keep a raw boto3 ``Table`` branch on this."""
    return os.getenv(METADATA_BACKEND_ENV, "").strip().lower() == "aurora"


def metadata_resource() -> Any:
    """A ``boto3.resource("dynamodb")`` stand-in whose ``.Table(name)`` is Aurora-backed (Aurora backend only)."""
    from coa_common.dao.aurora import AuroraMetadataResource

    return AuroraMetadataResource()


def make_dao(
    table_name: str,
    *,
    region: str,
    config: Config | None = None,
    dynamodb_cls: Callable[..., DatastoreDAO] | None = None,
) -> DatastoreDAO:
    """A DAO for the logical table ``table_name``: DynamoDB by default, Aurora with ``METADATA_BACKEND=aurora``.

    ``region`` and ``config`` apply to DynamoDB only; the Aurora DAO connects through the Data API settings
    in the environment (``AURORA_CLUSTER_ARN``, ``AURORA_SECRET_ARN``).
    ``dynamodb_cls`` replaces the DynamoDB constructor, so a caller that binds ``DynamoDBDAO`` in its own
    module keeps that name patchable; with it, ``config`` is forwarded only when given.

    Raises:
        ValueError: ``METADATA_BACKEND`` holds a value other than ``dynamodb`` or ``aurora``.
    """
    backend = os.getenv(METADATA_BACKEND_ENV, "").strip().lower()
    if backend == "aurora":
        from coa_common.dao.aurora import AuroraMetadataDao

        return AuroraMetadataDao(table_name)
    if backend not in ("", "dynamodb"):
        raise ValueError(f"Unknown {METADATA_BACKEND_ENV} {backend!r}. Expected one of: dynamodb, aurora.")
    if dynamodb_cls is not None:
        if config is None:
            return dynamodb_cls(table_name, region=region)
        return dynamodb_cls(table_name, region=region, config=config)
    return DynamoDBDAO(table_name, region=region, config=config)


__all__ = [
    "DatastoreDAO",
    "DynamoDBDAO",
    "PaginatedResult",
    "QueryParams",
    "aurora_metadata_enabled",
    "make_dao",
    "metadata_resource",
]
