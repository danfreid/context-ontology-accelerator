# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reference implementations for the parity tests.

* :func:`patch_neptune_with_rdflib` routes ``NeptuneDBGraphStore``'s four transport functions
  (``_sparql_query``, ``_sparql_update``, ``_gsp_post_turtle``, ``_gsp_get_turtle``) to an in-memory
  ``rdflib.Dataset``, so the real SPARQL the Neptune store writes is evaluated by rdflib.
* :class:`FakeAoss` stands in for ``AossVectorClient`` with brute-force cosine, so the real
  ``OpenSearchVectorStore`` runs unchanged on top of it.
"""

from __future__ import annotations

import json
import math
from typing import Any

import rdflib


class RdflibNeptune:
    """An in-memory quad store behind the Neptune transport functions."""

    def __init__(self) -> None:
        """Create an empty dataset."""
        self.ds = rdflib.Dataset()
        self.queries: list[str] = []

    def sparql_query(self, query: str) -> dict:
        """Evaluate a SELECT and return SPARQL JSON results."""
        self.queries.append(query)
        res = self.ds.query(query)
        return json.loads(res.serialize(format="json"))

    def sparql_update(self, update: str) -> None:
        """Apply a SPARQL update."""
        self.queries.append(update)
        self.ds.update(update)

    def gsp_post_turtle(self, graph_uri: str, turtle: str) -> dict:
        """Parse Turtle into a named graph."""
        self.ds.graph(rdflib.URIRef(graph_uri)).parse(data=turtle, format="turtle")
        return {"graph_uri": graph_uri, "bytes_loaded": len(turtle.encode("utf-8")), "status": "ok"}

    def gsp_get_turtle(self, graph_uri: str) -> str | None:
        """Serialize a named graph, or ``None`` when it does not exist."""
        ref = rdflib.URIRef(graph_uri)
        if ref not in {c.identifier for c in self.ds.contexts()}:
            return None
        return self.ds.graph(ref).serialize(format="turtle")


def patch_neptune_with_rdflib(monkeypatch: Any) -> RdflibNeptune:
    """Patch the Neptune transport functions to an in-memory dataset and return it."""
    from coa_ontology.stores import neptune_db_graph as mod

    backend = RdflibNeptune()
    monkeypatch.setattr(mod, "_sparql_query", backend.sparql_query)
    monkeypatch.setattr(mod, "_sparql_update", backend.sparql_update)
    monkeypatch.setattr(mod, "_gsp_post_turtle", backend.gsp_post_turtle)
    monkeypatch.setattr(mod, "_gsp_get_turtle", backend.gsp_get_turtle)
    monkeypatch.setattr(mod, "NDB_ENDPOINT", "http://neptune.invalid:8182")
    return backend


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


class FakeAoss:
    """The ``AossVectorClient`` surface ``OpenSearchVectorStore`` uses, over in-memory lists."""

    def __init__(self) -> None:
        """Start with no indexes."""
        self.indexes: dict[str, list[dict]] = {}

    @staticmethod
    def _match(doc: dict, filters: list[dict]) -> bool:
        for f in filters:
            if "term" in f:
                ((field, value),) = f["term"].items()
                if doc.get(field) != value:
                    return False
            else:  # pragma: no cover - not used by the store
                raise NotImplementedError(f)
        return True

    def ensure_index(self, name: str) -> str:
        """Create the index when missing."""
        self.indexes.setdefault(name, [])
        return name

    def index_document(self, index: str, doc: dict) -> None:
        """Append one document (AOSS cannot upsert)."""
        self.indexes.setdefault(index, []).append(dict(doc))

    def bulk_index(self, index: str, docs: list[dict]) -> None:
        """Append documents."""
        self.indexes.setdefault(index, []).extend(dict(d) for d in docs)

    def filter_search(
        self, index: str, filters: list[dict], size: int = 10, source: list[str] | None = None
    ) -> list[dict]:
        """Term-filtered listing."""
        out = [d for d in self.indexes.get(index, []) if self._match(d, filters)][:size]
        if source is not None:
            out = [{k: v for k, v in d.items() if k in source} for d in out]
        return [dict(d) for d in out]

    def iter_source_field(self, index: str, filters: list[dict], field: str) -> list[str]:
        """Values of one field for matching documents."""
        return [d[field] for d in self.indexes.get(index, []) if self._match(d, filters)]

    def knn_search(
        self, index: str, vector: list[float], top_k: int = 10, filters: list[dict] | None = None
    ) -> list[dict]:
        """Brute-force cosine, scored like OpenSearch ``cosinesimil`` (``(1 + cos) / 2``)."""
        scored = []
        for d in self.indexes.get(index, []):
            if filters and not self._match(d, filters):
                continue
            scored.append({**d, "_score": (1 + _cos(vector, d["embedding"])) / 2})
        scored.sort(key=lambda h: -h["_score"])
        return scored[:top_k]

    def delete_by_term(self, index: str, field: str, value: Any, and_filters: list[dict] | None = None) -> int:
        """Delete matching documents and return how many."""
        values = value if isinstance(value, list) else [value]
        docs = self.indexes.get(index, [])
        keep = []
        removed = 0
        for d in docs:
            if d.get(field) in values and self._match(d, and_filters or []):
                removed += 1
            else:
                keep.append(d)
        self.indexes[index] = keep
        return removed

    def delete_index(self, index: str) -> bool:
        """Drop an index; True when it existed."""
        return self.indexes.pop(index, None) is not None

    def health_check(self, index: str) -> dict:
        """Always healthy."""
        return {"status": "ok"}


def aurora_vector_store(client: Any, namespace: str, dimensions: int) -> Any:
    """An `AuroraVectorStore` (`search_doc`) bound to an existing client instead of the environment."""
    from coa_common.aurora_search import AuroraSearchIndex
    from coa_ontology.stores.aurora_vector import AuroraVectorStore

    store = AuroraVectorStore(namespace=namespace)
    store.dimensions = dimensions
    store._client = client
    store._index = AuroraSearchIndex(client, dimensions)
    return store
