# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Teardown helpers for the Aurora document graph and S3 Vectors indexes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_common.graphrag_pg import teardown

pytestmark = pytest.mark.unit


def _not_found(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "NotFoundException", "Message": "x"}}, op)


def test_index_name_matches_the_toolkit_scheme():
    assert teardown.s3_vector_index_name("chunk_abc_1", "graphrag") == "graphrag.chunk-abc-1"
    assert teardown.s3_vector_index_name("chunk_abc", "") == "chunk-abc"


def test_delete_tenant_rows_batches_until_a_short_page():
    client = MagicMock()
    client.execute.side_effect = [
        [{"n": teardown.DELETE_BATCH}],
        [{"n": 3}],  # edge_src
        [{"n": 0}],  # edge_dst
        [{"n": 7}],  # node
    ]
    assert teardown.delete_tenant_rows(client, "lg", "t1") == {
        "edge_src": teardown.DELETE_BATCH + 3,
        "edge_dst": 0,
        "node": 7,
    }
    sqls = [c.args[0] for c in client.execute.call_args_list]
    assert "lg.edge" in sqls[0] and "e.src" in sqls[0] and "e.dst" in sqls[2] and "DELETE FROM lg.node" in sqls[3]
    assert all(c.args[1]["t"] == "t1" for c in client.execute.call_args_list)


@pytest.mark.parametrize(("schema", "tenant"), [("lg; drop", "t"), ("lg", "")])
def test_delete_tenant_rows_rejects_bad_input(schema, tenant):
    with pytest.raises(ValueError):
        teardown.delete_tenant_rows(MagicMock(), schema, tenant)


def test_existing_indexes_drops_only_not_found():
    s3 = MagicMock()
    s3.get_index.side_effect = [None, _not_found("GetIndex"), RuntimeError("boom")]
    assert teardown.existing_s3_vector_indexes(s3, "b", "graphrag", ["chunk_t", "topic_t", "statement_t"]) == [
        "chunk_t",
        "statement_t",
    ]


def test_delete_indexes_ignores_not_found_and_raises_otherwise():
    s3 = MagicMock()
    s3.delete_index.side_effect = [None, _not_found("DeleteIndex")]
    assert teardown.delete_s3_vector_indexes(s3, "b", "graphrag", ["chunk_t", "topic_t"]) == ["chunk_t"]
    s3.delete_index.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        teardown.delete_s3_vector_indexes(s3, "b", "graphrag", ["chunk_t"])
