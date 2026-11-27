# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""GRAPH_BACKEND=aurora / WORKBENCH_BACKEND=aurora_pgvector: accepting a proposal never calls Neptune.

* The S3 persist step reads the ontology Turtle from the active graph store, not the Neptune GSP endpoint.
* A bridge run without a ``neptune_endpoint`` skips the lexical source instead of failing.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _module(*names: str) -> types.ModuleType:
    mod = types.ModuleType("stub")
    for name in names:
        setattr(mod, name, MagicMock())
    return mod


TURTLE = "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n<http://ex.org/Claim> a owl:Class ."


class TestGraphTurtle:
    def test_aurora_backend_reads_the_aurora_graph_store(self, monkeypatch):
        from coa_ontology import proposals

        monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
        store = MagicMock()
        store.get_graph_turtle.return_value = TURTLE
        with (
            patch("coa_ontology.stores.build_stores", return_value=(store, MagicMock())) as build,
            patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", side_effect=AssertionError("Neptune")),
        ):
            assert proposals._graph_turtle("http://ex.org/o#", "ns", "https://graph/uri") == TURTLE
        build.assert_called_once_with(namespace="ns")
        store.get_graph_turtle.assert_called_once_with("http://ex.org/o#")

    def test_default_backend_still_reads_the_neptune_graph_store_protocol(self, monkeypatch):
        from coa_ontology import proposals

        monkeypatch.delenv("WORKBENCH_BACKEND", raising=False)
        with (
            patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", return_value=TURTLE) as gsp,
            patch("coa_ontology.stores.build_stores", side_effect=AssertionError("build_stores")),
        ):
            assert proposals._graph_turtle("http://ex.org/o#", "ns", "https://graph/uri") == TURTLE
        gsp.assert_called_once_with("https://graph/uri")

    @patch("boto3.client")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_persist_induced_to_s3_writes_the_aurora_turtle(self, mock_dynamo, mock_s3_client, monkeypatch):
        from coa_ontology.proposals import _persist_induced_to_s3

        monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
        mock_dynamo.list_proposals.return_value = []
        s3 = MagicMock()
        mock_s3_client.return_value = s3
        store = MagicMock()
        store.get_graph_turtle.return_value = TURTLE
        with (
            patch("coa_ontology.stores.build_stores", return_value=(store, MagicMock())),
            patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", side_effect=AssertionError("Neptune")),
        ):
            _persist_induced_to_s3("http://ex.org/o#", "my-namespace", "https://graph/uri")
        bodies = [c.kwargs["Body"] for c in s3.put_object.call_args_list]
        assert any(b"owl:Class" in b for b in bodies)

    @patch("boto3.client")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_persist_skips_when_the_aurora_graph_is_empty(self, mock_dynamo, mock_s3_client, monkeypatch):
        from coa_ontology.proposals import _persist_induced_to_s3

        monkeypatch.setenv("WORKBENCH_BACKEND", "aurora_pgvector")
        s3 = MagicMock()
        mock_s3_client.return_value = s3
        store = MagicMock()
        store.get_graph_turtle.return_value = None
        with patch("coa_ontology.stores.build_stores", return_value=(store, MagicMock())):
            _persist_induced_to_s3("http://ex.org/o#", "ns", "https://graph/uri")
        s3.put_object.assert_not_called()


class TestBridgeWithoutNeptune:
    def test_empty_neptune_endpoint_skips_the_lexical_source(self):
        from coa_ontology.inducer.bridge import service
        from coa_ontology.inducer.bridge.planner import BridgeOptions

        captured = {}
        unstructured = types.ModuleType("coa_ontology.inducer.routers.induce_unstructured")
        unstructured._build_lexical_store = MagicMock(side_effect=AssertionError("the lexical store must not be built"))  # type: ignore[attr-defined]

        def fake_run_bridge(**kwargs):
            captured["deps"] = kwargs["deps"]
            return {"notes": []}

        with (
            patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())),
            patch("coa_ontology.proposals._accumulated_r2rml", return_value=""),
            patch.dict(
                sys.modules,
                {
                    "coa_ontology.inducer.routers.induce_unstructured": unstructured,
                    "coa_ontology.stores.adapters": _module("StoreOntologyCatalogAdapter"),
                    "coa_ontology.bedrock_embeddings": _module("BedrockEmbeddingClient"),
                    "coa_ontology.inducer.services.embedding_generator": _module("EmbeddingGeneratorClient"),
                    "coa_ontology.inducer.services.grounding": _module("GroundingService"),
                },
            ),
            patch.object(service, "BridgeLLM"),
            patch.object(service, "_chunk_searcher", return_value=None),
            patch.object(service, "run_bridge", side_effect=fake_run_bridge),
        ):
            report = service.run_bridge_for_namespace(
                "ns", "http://ex.org/o#", BridgeOptions(), config={"neptune_endpoint": ""}
            )
        assert captured["deps"].lexical_store is None
        assert "notes" not in report or not any("lexical" in n for n in report["notes"])
