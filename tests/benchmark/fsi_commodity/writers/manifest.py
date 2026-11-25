# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic JSON and file output helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def dumps_json(obj: Any) -> bytes:
    """Serialise with sorted keys, 2-space indent, ``\\n`` newlines and a trailing newline."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` in binary mode (no newline translation), creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
