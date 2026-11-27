# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only schema check for the Aurora graph and metadata stores."""

from __future__ import annotations

import pytest
from coa_ontology.stores.aurora import schema
from coa_ontology.stores.aurora.schema import AuroraSchemaError, apply_schema_for_dev, check_schema

from .aurora_fakes import SCHEMA_ROW, ScriptedRds

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    schema.reset_schema_cache()


def test_check_passes_and_is_read_only_and_cached():
    rds = ScriptedRds()
    client = rds.client()
    assert check_schema(client, 8).dimensions == 8
    check_schema(client, 8)
    assert len(rds.sql_calls("to_regclass")) == 1
    assert all(c["sql"].lstrip().upper().startswith("SELECT") for c in rds.calls)


def test_missing_table_names_the_init_script():
    rds = ScriptedRds(schema_row={**SCHEMA_ROW, "has_triple": False})
    with pytest.raises(AuroraSchemaError, match=r"coa_triple.*aurora-init\.sql"):
        check_schema(rds.client(), 8)


def test_dimension_mismatch_is_rejected():
    rds = ScriptedRds()
    rds.when("vector_dims", [{"dimensions": 1024}])
    with pytest.raises(AuroraSchemaError, match="1024"):
        check_schema(rds.client(), 8)


def test_matching_dimension_is_accepted():
    rds = ScriptedRds()
    rds.when("vector_dims", [{"dimensions": 8}])
    assert check_schema(rds.client(), 8).stored_dimensions == 8


def test_apply_schema_for_dev_runs_ddl_in_one_transaction():
    rds = ScriptedRds()
    apply_schema_for_dev(rds.client())
    assert rds.events == ["begin", "commit"]
    assert rds.sql_calls("CREATE TABLE IF NOT EXISTS coa_triple")
    assert rds.sql_calls("CREATE TABLE IF NOT EXISTS ontology_engine")
    assert not rds.sql_calls("coa_embedding")
