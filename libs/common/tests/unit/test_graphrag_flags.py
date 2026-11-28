# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Flag helper for the Aurora document graph + S3 Vectors path."""

from __future__ import annotations

import pytest
from coa_common.constants import (
    ckg_path_enabled,
    graphrag_aurora_graph_uri,
    graphrag_s3_vector_uri,
    graphrag_vector_backend,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GRAPH_BACKEND",
        "GRAPHRAG_VECTOR_BACKEND",
        "GRAPHRAG_S3_VECTOR_BUCKET",
        "GRAPHRAG_S3_VECTOR_PREFIX",
        "GRAPHRAG_AURORA_SCHEMA",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_is_aoss_and_path_disabled() -> None:
    assert graphrag_vector_backend() == "aoss"
    assert ckg_path_enabled() is False


@pytest.mark.parametrize(
    ("graph", "vector", "expected"),
    [
        ("aurora", "s3vectors", True),
        (" Aurora ", "S3Vectors", True),
        ("aurora", "", False),
        ("aurora", "aoss", False),
        ("", "s3vectors", False),
        ("neptune", "s3vectors", False),
    ],
)
def test_predicate_needs_both_flags(monkeypatch: pytest.MonkeyPatch, graph: str, vector: str, expected: bool) -> None:
    monkeypatch.setenv("GRAPH_BACKEND", graph)
    monkeypatch.setenv("GRAPHRAG_VECTOR_BACKEND", vector)
    assert ckg_path_enabled() is expected


def test_uris(monkeypatch: pytest.MonkeyPatch) -> None:
    assert graphrag_aurora_graph_uri() == "aurora-pg://?schema=lg"
    with pytest.raises(ValueError, match="GRAPHRAG_S3_VECTOR_BUCKET"):
        graphrag_s3_vector_uri()
    monkeypatch.setenv("GRAPHRAG_S3_VECTOR_BUCKET", "b")
    assert graphrag_s3_vector_uri() == "s3vectors://b/graphrag"
    monkeypatch.setenv("GRAPHRAG_S3_VECTOR_PREFIX", "/p/")
    monkeypatch.setenv("GRAPHRAG_AURORA_SCHEMA", "lgx")
    assert graphrag_s3_vector_uri() == "s3vectors://b/p"
    assert graphrag_aurora_graph_uri() == "aurora-pg://?schema=lgx"
