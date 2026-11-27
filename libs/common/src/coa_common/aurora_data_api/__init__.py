# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reusable RDS Data API access layer for Aurora PostgreSQL.

Import from here (``from coa_common.aurora_data_api import AuroraDataApiClient``);
``coa_common`` re-exports ``AuroraDataApiClient`` lazily. ``DataApiClient`` is an alias.
"""

from coa_common.aurora_data_api.client import (
    JSON_CHUNK_BYTES,
    AuroraDataApiClient,
    DataApiClient,
    Transaction,
    build_client_from_env,
    bulk_json_chunks,
    json_text,
    to_data_api_params,
)
from coa_common.aurora_data_api.config import AuroraConfig, clear_ssm_cache
from coa_common.aurora_data_api.errors import (
    AuroraAccessError,
    AuroraConfigError,
    AuroraDataApiError,
    AuroraQueryError,
    AuroraResponseTooLargeError,
    AuroraTimeoutError,
    AuroraUnavailableError,
    ResponseTooLargeError,
    ResumeTimeoutError,
    StatementTimeoutError,
)

__all__ = [
    "JSON_CHUNK_BYTES",
    "AuroraAccessError",
    "AuroraConfig",
    "AuroraConfigError",
    "AuroraDataApiClient",
    "AuroraDataApiError",
    "AuroraQueryError",
    "AuroraResponseTooLargeError",
    "AuroraTimeoutError",
    "AuroraUnavailableError",
    "DataApiClient",
    "ResponseTooLargeError",
    "ResumeTimeoutError",
    "StatementTimeoutError",
    "Transaction",
    "build_client_from_env",
    "bulk_json_chunks",
    "clear_ssm_cache",
    "json_text",
    "to_data_api_params",
]
