# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Search backend selection.

``resolve_search_backend`` reads only environment variables, so the default
(AOSS) path never makes an AWS call. Connection settings (``AuroraConfig``) live in
:mod:`coa_common.aurora_data_api.config`; this module re-exports them.

Environment variables:
    HYBRID_SEARCH_BACKEND: ``aoss`` (default) or ``pgvector+tsvector`` / ``pgvector``.
    WORKBENCH_BACKEND: ``aurora_pgvector`` also selects the Aurora backend.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum

from coa_common.aurora_data_api.config import (
    DEFAULT_DATABASE,
    DEFAULT_DIMENSIONS,
    MAX_DIMENSIONS,
    MIN_DIMENSIONS,
    AuroraConfig,
)

__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_DIMENSIONS",
    "MAX_DIMENSIONS",
    "MIN_DIMENSIONS",
    "AuroraConfig",
    "SearchBackend",
    "resolve_search_backend",
]


class SearchBackend(StrEnum):
    """Which engine serves vector and keyword search."""

    AOSS = "aoss"
    PGVECTOR = "pgvector+tsvector"


def resolve_search_backend(env: Mapping[str, str] | None = None) -> SearchBackend:
    """Pick the search backend from the environment. Defaults to AOSS."""
    env = os.environ if env is None else env
    explicit = (env.get("HYBRID_SEARCH_BACKEND") or "").strip().lower()
    if explicit in ("pgvector+tsvector", "pgvector"):
        return SearchBackend.PGVECTOR
    if explicit:
        return SearchBackend.AOSS
    if (env.get("WORKBENCH_BACKEND") or "").strip().lower() == "aurora_pgvector":
        return SearchBackend.PGVECTOR
    return SearchBackend.AOSS
