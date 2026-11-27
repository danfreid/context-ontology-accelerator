# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data Access Object pattern: ABC, DynamoDB implementation, Aurora implementation and the backend switch."""

import os

from botocore.config import Config

from coa_common.dao.base import DatastoreDAO, PaginatedResult, QueryParams
from coa_common.dao.dynamodb import DynamoDBDAO

METADATA_BACKEND_ENV = "METADATA_BACKEND"


def make_dao(table_name: str, *, region: str, config: Config | None = None) -> DatastoreDAO:
    """A DAO for the logical table ``table_name``: DynamoDB by default, Aurora with ``METADATA_BACKEND=aurora``.

    ``region`` and ``config`` apply to DynamoDB only; the Aurora DAO connects through the Data API settings
    in the environment (``AURORA_CLUSTER_ARN``, ``AURORA_SECRET_ARN``).

    Raises:
        ValueError: ``METADATA_BACKEND`` holds a value other than ``dynamodb`` or ``aurora``.
    """
    backend = os.getenv(METADATA_BACKEND_ENV, "").strip().lower()
    if backend == "aurora":
        from coa_common.dao.aurora import AuroraMetadataDao

        return AuroraMetadataDao(table_name)
    if backend not in ("", "dynamodb"):
        raise ValueError(f"Unknown {METADATA_BACKEND_ENV} {backend!r}. Expected one of: dynamodb, aurora.")
    return DynamoDBDAO(table_name, region=region, config=config)


__all__ = [
    "DatastoreDAO",
    "DynamoDBDAO",
    "PaginatedResult",
    "QueryParams",
    "make_dao",
]
