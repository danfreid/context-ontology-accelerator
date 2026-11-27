# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Search backend selection and Aurora connection settings.

``resolve_search_backend`` reads only environment variables, so the default
(AOSS) path never makes an AWS call.

Environment variables:
    HYBRID_SEARCH_BACKEND: ``aoss`` (default) or ``pgvector+tsvector`` / ``pgvector``.
    WORKBENCH_BACKEND: ``aurora_pgvector`` also selects the Aurora backend.
    AURORA_CLUSTER_ARN: ARN, or SSM parameter name (leading ``/``).
    AURORA_SECRET_ARN: required. ARN, or SSM parameter name (leading ``/``). There is no fallback,
        so a workload cannot silently pick up the admin secret.
    AURORA_SSM_PREFIX: prefix for ``/aurora/cluster-arn``.
    AURORA_DATABASE: database name (default ``postgres``).
    AURORA_VECTOR_DIMENSIONS, OSS_DIMENSIONS: embedding dimension (default 1024).
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

DEFAULT_DIMENSIONS = 1024
DEFAULT_DATABASE = "postgres"
MIN_DIMENSIONS = 1
MAX_DIMENSIONS = 2000  # HNSW on the ``vector`` type indexes at most 2000 dimensions


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


@functools.lru_cache(maxsize=32)
def _ssm_get(name: str) -> str:
    import boto3

    from coa_common.aws_config import sync_boto_config

    response = boto3.client("ssm", config=sync_boto_config()).get_parameters(Names=[name])
    for param in response.get("Parameters", []):
        if param.get("Name") == name:
            return str(param["Value"])
    raise RuntimeError(f"SSM parameter {name} not found")


def _resolve(value: str) -> str:
    """A value starting with '/' is an SSM parameter name; anything else is used as is."""
    return _ssm_get(value) if value.startswith("/") else value


@dataclass(frozen=True)
class AuroraConfig:
    """Connection settings for the Aurora Data API."""

    cluster_arn: str
    secret_arn: str
    database: str = DEFAULT_DATABASE
    dimensions: int = DEFAULT_DIMENSIONS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuroraConfig:
        """Build the config from env vars. Only the cluster ARN falls back to SSM under AURORA_SSM_PREFIX."""
        env = os.environ if env is None else env
        prefix = (env.get("AURORA_SSM_PREFIX") or "").rstrip("/")

        def pick(var: str, ssm_leaf: str) -> str:
            value = env.get(var)
            if value:
                return _resolve(value)
            if prefix:
                return _ssm_get(f"{prefix}/aurora/{ssm_leaf}")
            raise RuntimeError(f"Aurora search needs {var} (or AURORA_SSM_PREFIX) to be set")

        cluster_arn = pick("AURORA_CLUSTER_ARN", "cluster-arn")
        secret_value = env.get("AURORA_SECRET_ARN")
        if not secret_value:
            raise RuntimeError("Aurora search needs AURORA_SECRET_ARN (the search role secret); it has no fallback")
        secret_arn = _resolve(secret_value)
        raw_dim = env.get("AURORA_VECTOR_DIMENSIONS") or env.get("OSS_DIMENSIONS") or str(DEFAULT_DIMENSIONS)
        try:
            dimensions = int(raw_dim)
        except ValueError as exc:
            raise RuntimeError(f"AURORA_VECTOR_DIMENSIONS must be an integer, got {raw_dim!r}") from exc
        if not MIN_DIMENSIONS <= dimensions <= MAX_DIMENSIONS:
            raise RuntimeError(
                f"Aurora vector dimensions must be between {MIN_DIMENSIONS} and {MAX_DIMENSIONS}, got {dimensions}"
            )
        return cls(
            cluster_arn=cluster_arn,
            secret_arn=secret_arn,
            database=env.get("AURORA_DATABASE") or DEFAULT_DATABASE,
            dimensions=dimensions,
        )
