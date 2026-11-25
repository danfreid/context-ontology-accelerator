# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora PostgreSQL search layer: hybrid vector + BM25 + trigram search over the RDS Data API.

Alternative to the OpenSearch Serverless (AOSS) path. Select it with
``resolve_search_backend()``; the default stays AOSS.
"""

from __future__ import annotations

from coa_common.aurora_search.bm25 import bm25_score, idf, rank_bm25
from coa_common.aurora_search.config import AuroraConfig, SearchBackend, resolve_search_backend
from coa_common.aurora_search.data_api import (
    AuroraAccessError,
    AuroraDataApiClient,
    AuroraDataApiError,
    AuroraQueryError,
    AuroraResponseTooLargeError,
    AuroraTimeoutError,
    AuroraUnavailableError,
)
from coa_common.aurora_search.fusion import FusedHit, SearchMode, reciprocal_rank_fusion
from coa_common.aurora_search.index import AuroraSearchIndex, FuzzyHit, SearchDoc, SearchHit

__all__ = [
    "AuroraAccessError",
    "AuroraConfig",
    "AuroraDataApiClient",
    "AuroraDataApiError",
    "AuroraQueryError",
    "AuroraResponseTooLargeError",
    "AuroraSearchIndex",
    "AuroraTimeoutError",
    "AuroraUnavailableError",
    "FusedHit",
    "FuzzyHit",
    "SearchBackend",
    "SearchDoc",
    "SearchHit",
    "SearchMode",
    "bm25_score",
    "idf",
    "rank_bm25",
    "reciprocal_rank_fusion",
    "resolve_search_backend",
]
