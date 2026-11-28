# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Time the hard reads (topic neighbours, statements by topic and source) on a large synthetic graph."""

from __future__ import annotations

import random
import sys
import time

from coa_common.graphrag_pg.store import AuroraGraphStore, register
from graphrag_toolkit.lexical_graph.retrieval.processors import ProcessorArgs
from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch

schema = sys.argv[1] if len(sys.argv) > 1 else "lgstress"
register()
store = AuroraGraphStore(schema_name=schema)
topics = [
    r["id"]
    for r in store.client.execute(f"SELECT id FROM {schema}.node WHERE label = '__Topic__' ORDER BY random() LIMIT 400")
]
random.seed(1)

tb = object.__new__(TopicBeamSearch)
tb.graph_store = store
tb.use_entity_neighbors = True
tb.use_same_chunk_neighbors = True
tb.use_adjacent_chunk_neighbors = True
tb.max_entity_neighbors = 20
tb.max_chunk_neighbors = 20
tb.max_statements_per_topic = 25
tb.args = ProcessorArgs(include_chunk_details=True)

for n in (1, 10, 100, 400):
    t0 = time.time()
    res = tb.get_neighbors_batch(topics[:n])
    print(
        f"neighbors topics={n} -> {sum(len(v) for v in res.values())} neighbour ids in {time.time() - t0:.2f}s",
        flush=True,
    )

for n in (25, 100, 400):
    t0 = time.time()
    st = tb._expand_topics_to_statement_ids(topics[:n])
    t1 = time.time()
    results = tb.get_statements_by_topic_and_source(list(set(st)))
    print(
        f"expand topics={n} -> {len(st)} statements ({t1 - t0:.2f}s); statements_by_topic_and_source -> "
        f"{len(results)} sources in {time.time() - t1:.2f}s",
        flush=True,
    )
