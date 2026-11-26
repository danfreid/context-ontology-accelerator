# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Connection settings for the Aurora RDS Data API.

The Data API needs a cluster ARN and a Secrets Manager secret ARN. Deployments
that expose these through SSM parameter names (the CDK stacks pass
``AURORA_CLUSTER_ENDPOINT`` and ``AURORA_SECRET_ARN`` as SSM names) are handled
here: a value that starts with ``/`` is read from SSM, lazily and once.

Resolution order for the cluster ARN:

1. ``AURORA_CLUSTER_ARN`` (literal ARN, preferred)
2. ``AURORA_RESOURCE_ARN`` (literal ARN)
3. ``AURORA_CLUSTER_ENDPOINT`` when it starts with ``/``: the sibling SSM
   parameter ``<same prefix>/cluster-arn``

The secret comes from ``AURORA_SECRET_ARN``: used as is when it starts with
``arn:``, otherwise read as an SSM parameter name. Credentials are never read
here; the Data API reads the secret itself.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from coa_common.aurora_data_api.errors import AuroraConfigError

DEFAULT_DATABASE = "postgres"
DEFAULT_RESUME_TIMEOUT_SECONDS = 45.0
DEFAULT_DIMENSIONS = 1024
MIN_DIMENSIONS = 1
MAX_DIMENSIONS = 2000  # HNSW on the ``vector`` type indexes at most 2000 dimensions

_ssm_cache: dict[str, str] = {}
_ssm_lock = threading.Lock()


def clear_ssm_cache() -> None:
    """Forget cached SSM values (tests, or after a stack redeploy)."""
    with _ssm_lock:
        _ssm_cache.clear()


def _read_ssm(name: str, ssm_client: Any | None, region: str | None) -> str:
    """Return the value of SSM parameter ``name``, cached per process."""
    with _ssm_lock:
        if name in _ssm_cache:
            return _ssm_cache[name]
    client = ssm_client or boto3.client("ssm", region_name=region)
    try:
        value = client.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDeniedException", "AccessDenied"}:
            raise AuroraConfigError(
                f"Cannot read SSM parameter {name!r}: the role lacks ssm:GetParameter on it. "
                "Grant that permission, or set AURORA_CLUSTER_ARN and a literal AURORA_SECRET_ARN instead."
            ) from exc
        if code == "ParameterNotFound":
            raise AuroraConfigError(f"SSM parameter {name!r} does not exist") from exc
        raise AuroraConfigError(f"Cannot read SSM parameter {name!r}: {code or exc}") from exc
    with _ssm_lock:
        _ssm_cache[name] = value
    return value


@dataclass(frozen=True)
class AuroraConfig:
    """Resolved Data API connection settings."""

    resource_arn: str
    secret_arn: str
    database: str = DEFAULT_DATABASE
    resume_timeout_seconds: float = DEFAULT_RESUME_TIMEOUT_SECONDS
    region: str | None = None
    dimensions: int = DEFAULT_DIMENSIONS

    @property
    def cluster_arn(self) -> str:
        """Alias of :attr:`resource_arn` (the name the search layer uses)."""
        return self.resource_arn

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, ssm_client: Any | None = None) -> AuroraConfig:
        """Build the config from environment variables, reading SSM when a value is a parameter name.

        Raises:
            AuroraConfigError: A required value is missing, or an SSM read failed.
        """
        e = os.environ if env is None else env
        region = e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or None
        prefix = (e.get("AURORA_SSM_PREFIX") or "").strip().rstrip("/")
        resource_arn = (e.get("AURORA_CLUSTER_ARN") or e.get("AURORA_RESOURCE_ARN") or "").strip()
        if resource_arn.startswith("/"):
            resource_arn = _read_ssm(resource_arn, ssm_client, region)
        if not resource_arn:
            endpoint = (e.get("AURORA_CLUSTER_ENDPOINT") or "").strip()
            if endpoint.startswith("/"):
                resource_arn = _read_ssm(f"{endpoint.rsplit('/', 1)[0]}/cluster-arn", ssm_client, region)
            elif prefix:
                resource_arn = _read_ssm(f"{prefix}/aurora/cluster-arn", ssm_client, region)
            else:
                raise AuroraConfigError(
                    "Aurora cluster ARN not configured: set AURORA_CLUSTER_ARN (or AURORA_RESOURCE_ARN), "
                    "AURORA_SSM_PREFIX, or AURORA_CLUSTER_ENDPOINT to an SSM parameter name that has a sibling "
                    "'cluster-arn' parameter."
                )
        secret = (e.get("AURORA_SECRET_ARN") or "").strip()
        if not secret and not prefix:
            raise AuroraConfigError("AURORA_SECRET_ARN is not set (or set AURORA_SSM_PREFIX)")
        if not secret:
            secret = f"{prefix}/aurora/secret-arn"
        secret_arn = secret if secret.startswith("arn:") else _read_ssm(secret, ssm_client, region)
        try:
            timeout = float(e.get("AURORA_RESUME_TIMEOUT_SECONDS") or DEFAULT_RESUME_TIMEOUT_SECONDS)
        except ValueError as exc:
            raise AuroraConfigError("AURORA_RESUME_TIMEOUT_SECONDS must be a number") from exc
        raw_dim = (e.get("AURORA_VECTOR_DIMENSIONS") or e.get("OSS_DIMENSIONS") or str(DEFAULT_DIMENSIONS)).strip()
        try:
            dimensions = int(raw_dim)
        except ValueError as exc:
            raise AuroraConfigError(f"AURORA_VECTOR_DIMENSIONS must be an integer, got {raw_dim!r}") from exc
        if not MIN_DIMENSIONS <= dimensions <= MAX_DIMENSIONS:
            raise AuroraConfigError(
                f"Aurora vector dimensions must be between {MIN_DIMENSIONS} and {MAX_DIMENSIONS}, got {dimensions}"
            )
        return cls(
            resource_arn=resource_arn,
            secret_arn=secret_arn,
            database=(e.get("AURORA_DATABASE") or DEFAULT_DATABASE).strip(),
            resume_timeout_seconds=timeout,
            region=region,
            dimensions=dimensions,
        )

    def __repr__(self) -> str:
        """Hide the ARNs so the config is safe to log."""
        return (
            f"AuroraConfig(database={self.database!r}, "
            f"resume_timeout_seconds={self.resume_timeout_seconds}, dimensions={self.dimensions})"
        )
