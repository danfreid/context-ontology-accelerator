# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Errors raised by the Aurora RDS Data API layer."""

from __future__ import annotations


class AuroraDataApiError(Exception):
    """Base class for every error this package raises on purpose."""


class AuroraConfigError(AuroraDataApiError):
    """Connection settings are missing or cannot be resolved."""


class AuroraQueryError(AuroraDataApiError):
    """The statement was rejected (``BadRequestException``: bad SQL, constraint violation, wrong parameter)."""


class AuroraAccessError(AuroraDataApiError):
    """The caller may not use the cluster, secret or database."""


class AuroraUnavailableError(AuroraDataApiError):
    """The cluster or Data API is unavailable, including a pause/resume that did not finish."""


class ResponseTooLargeError(AuroraDataApiError):
    """The result set exceeded the Data API 1 MiB response limit."""


class ResumeTimeoutError(AuroraUnavailableError):
    """The cluster stayed paused (``DatabaseResumingException``) longer than the retry budget."""


class StatementTimeoutError(AuroraDataApiError):
    """The statement ran past the Data API 45 second limit. Never retried."""


# Names used by the aurora_search layer.
AuroraResponseTooLargeError = ResponseTooLargeError
AuroraTimeoutError = StatementTimeoutError
