# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reusable RDS Data API access layer for Aurora PostgreSQL.

Import from here (``from coa_common.aurora_data_api import DataApiClient``); the
top-level ``coa_common`` package does not re-export it.
"""

from coa_common.aurora_data_api.client import (
    JSON_CHUNK_BYTES,
    DataApiClient,
    Transaction,
    bulk_json_chunks,
    json_text,
    to_data_api_params,
)
from coa_common.aurora_data_api.config import AuroraConfig, clear_ssm_cache
from coa_common.aurora_data_api.errors import (
    AuroraConfigError,
    AuroraDataApiError,
    ResponseTooLargeError,
    ResumeTimeoutError,
    StatementTimeoutError,
)

__all__ = [
    "JSON_CHUNK_BYTES",
    "AuroraConfig",
    "AuroraConfigError",
    "AuroraDataApiError",
    "DataApiClient",
    "ResponseTooLargeError",
    "ResumeTimeoutError",
    "StatementTimeoutError",
    "Transaction",
    "bulk_json_chunks",
    "clear_ssm_cache",
    "json_text",
    "to_data_api_params",
]
