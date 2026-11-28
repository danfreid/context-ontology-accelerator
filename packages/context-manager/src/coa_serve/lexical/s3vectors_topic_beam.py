# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``TopicBeamSearch`` that seeds through ``VectorIndex.top_k`` so it runs on S3 Vectors.

graphrag-lexical-graph 3.20.0 ``TopicBeamSearch.get_start_node_ids`` calls
``topic_index.client.query(...)``, an OpenSearch/llama-index vector-store call.
``S3VectorIndex.client`` is the boto3 ``s3vectors`` client, which has no ``query``.
This subclass seeds with the backend-neutral ``top_k`` instead and leaves the beam
search untouched (its neighbour embeddings already come from ``get_embeddings``).

Importing this module imports graphrag_toolkit. Import it lazily (see
``strategies._build_topic_beam_engine``), never at module load of ``coa_serve``.
"""

from __future__ import annotations

import numpy as np
from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch
from graphrag_toolkit.lexical_graph.storage.vector.vector_index import to_embedded_query
from llama_index.core import QueryBundle


class S3VectorsTopicBeamSearch(TopicBeamSearch):
    """Seed topics with ``VectorIndex.top_k`` instead of ``topic_index.client.query``."""

    def get_start_node_ids(self, query_bundle: QueryBundle) -> list[str]:
        """Return the ``top_k`` nearest topic ids for the query."""
        self._topic_cache.clear()
        topic_index = self.vector_store.get_index("topic")
        query_bundle = to_embedded_query(query_bundle, topic_index.embed_model)
        self._query_embedding = np.array(query_bundle.embedding)
        results = topic_index.top_k(query_bundle, top_k=self.top_k)
        return [r["topic"]["topicId"] for r in results if (r.get("topic") or {}).get("topicId")]
