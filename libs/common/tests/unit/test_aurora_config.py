# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for search backend selection. ``AuroraConfig`` is covered in test_aurora_data_api_config.py."""

from __future__ import annotations

import pytest
from coa_common.aurora_search.config import AuroraConfig, SearchBackend, resolve_search_backend

pytestmark = pytest.mark.unit


def test_empty_env_is_aoss() -> None:
    assert resolve_search_backend({}) == SearchBackend.AOSS


@pytest.mark.parametrize("value", ["pgvector+tsvector", "pgvector", "PGVECTOR", " PgVector+TsVector "])
def test_hybrid_backend_values_select_pgvector(value: str) -> None:
    assert resolve_search_backend({"HYBRID_SEARCH_BACKEND": value}) == SearchBackend.PGVECTOR


@pytest.mark.parametrize("value", ["aoss", "AOSS", "garbage", "opensearch", ""])
def test_other_hybrid_values_are_aoss(value: str) -> None:
    assert resolve_search_backend({"HYBRID_SEARCH_BACKEND": value}) == SearchBackend.AOSS


def test_workbench_backend_selects_pgvector() -> None:
    assert resolve_search_backend({"WORKBENCH_BACKEND": "aurora_pgvector"}) == SearchBackend.PGVECTOR


def test_hybrid_env_wins_over_workbench() -> None:
    env = {"HYBRID_SEARCH_BACKEND": "aoss", "WORKBENCH_BACKEND": "aurora_pgvector"}
    assert resolve_search_backend(env) == SearchBackend.AOSS


def test_garbage_workbench_backend_is_aoss() -> None:
    assert resolve_search_backend({"WORKBENCH_BACKEND": "neptune"}) == SearchBackend.AOSS


def test_resolve_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYBRID_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
    assert resolve_search_backend() == SearchBackend.AOSS
    monkeypatch.setenv("HYBRID_SEARCH_BACKEND", "pgvector")
    assert resolve_search_backend() == SearchBackend.PGVECTOR


def test_import_coa_common_is_lazy() -> None:
    import coa_common

    assert coa_common.resolve_search_backend({}) == SearchBackend.AOSS
    assert coa_common.AuroraSearchIndex.__name__ == "AuroraSearchIndex"


def test_aurora_config_is_the_shared_class() -> None:
    from coa_common.aurora_data_api import AuroraConfig as Shared

    assert AuroraConfig is Shared
