# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for search backend selection and Aurora config resolution."""

from __future__ import annotations

import pytest
from coa_common.aurora_search import config
from coa_common.aurora_search.config import AuroraConfig, SearchBackend, resolve_search_backend

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fake_ssm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    config._ssm_get.cache_clear()
    calls: list[str] = []

    def fake(name: str) -> str:
        calls.append(name)
        return f"resolved:{name}"

    monkeypatch.setattr(config, "_ssm_get", fake)
    return calls


def test_empty_env_is_aoss(fake_ssm: list[str]) -> None:
    assert resolve_search_backend({}) == SearchBackend.AOSS
    assert fake_ssm == []


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


def test_from_env_uses_arns_without_ssm(fake_ssm: list[str]) -> None:
    cfg = AuroraConfig.from_env({"AURORA_CLUSTER_ARN": "arn:aws:rds:c", "AURORA_SECRET_ARN": "arn:aws:secret:s"})
    assert (cfg.cluster_arn, cfg.secret_arn) == ("arn:aws:rds:c", "arn:aws:secret:s")
    assert cfg.database == "postgres"
    assert cfg.dimensions == 1024
    assert fake_ssm == []


def test_from_env_falls_back_to_ssm_prefix(fake_ssm: list[str]) -> None:
    cfg = AuroraConfig.from_env({"AURORA_SSM_PREFIX": "/coa/dev/"})
    assert fake_ssm == ["/coa/dev/aurora/cluster-arn", "/coa/dev/aurora/secret-arn"]
    assert cfg.cluster_arn == "resolved:/coa/dev/aurora/cluster-arn"


def test_slash_prefixed_secret_is_ssm_name(fake_ssm: list[str]) -> None:
    cfg = AuroraConfig.from_env(
        {"AURORA_CLUSTER_ARN": "arn:aws:rds:c", "AURORA_SECRET_ARN": "/coa/dev/aurora/secret-arn"}
    )
    assert cfg.secret_arn == "resolved:/coa/dev/aurora/secret-arn"
    assert cfg.cluster_arn == "arn:aws:rds:c"
    assert fake_ssm == ["/coa/dev/aurora/secret-arn"]


def test_missing_settings_raise_clear_error() -> None:
    with pytest.raises(RuntimeError, match="AURORA_CLUSTER_ARN"):
        AuroraConfig.from_env({})
    with pytest.raises(RuntimeError, match="AURORA_SECRET_ARN"):
        AuroraConfig.from_env({"AURORA_CLUSTER_ARN": "arn:c"})


def test_dimensions_and_database() -> None:
    base = {"AURORA_CLUSTER_ARN": "arn:c", "AURORA_SECRET_ARN": "arn:s"}
    assert AuroraConfig.from_env({**base, "OSS_DIMENSIONS": "768"}).dimensions == 768
    assert AuroraConfig.from_env({**base, "OSS_DIMENSIONS": "768", "AURORA_VECTOR_DIMENSIONS": "256"}).dimensions == 256
    assert AuroraConfig.from_env({**base, "AURORA_DATABASE": "coa"}).database == "coa"
    with pytest.raises(RuntimeError, match="AURORA_VECTOR_DIMENSIONS"):
        AuroraConfig.from_env({**base, "AURORA_VECTOR_DIMENSIONS": "abc"})


def test_import_coa_common_is_lazy() -> None:
    import coa_common

    assert coa_common.resolve_search_backend({}) == SearchBackend.AOSS
    assert coa_common.AuroraSearchIndex.__name__ == "AuroraSearchIndex"
