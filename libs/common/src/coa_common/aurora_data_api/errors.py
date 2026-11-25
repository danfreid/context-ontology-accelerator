# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Errors raised by the Aurora RDS Data API layer."""

from __future__ import annotations


class AuroraDataApiError(Exception):
    """Base class for every error this package raises on purpose."""


class AuroraConfigError(AuroraDataApiError):
    """Connection settings are missing or cannot be resolved."""


class ResponseTooLargeError(AuroraDataApiError):
    """The result set exceeded the Data API 1 MiB response limit."""


class ResumeTimeoutError(AuroraDataApiError):
    """The cluster stayed paused (``DatabaseResumingException``) longer than the retry budget."""


class StatementTimeoutError(AuroraDataApiError):
    """The statement ran past the Data API 45 second limit. Never retried."""
