# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Spike driver: GraphRAG lexical graph in Aurora (Data API) + S3 Vectors, run from a laptop.

Subcommands: ``schema``, ``extract``, ``build``, ``query``, ``counts``, ``wipe``.

Environment (all required for AWS access):
  AURORA_CLUSTER_ARN, AURORA_SECRET_ARN, AWS_REGION
  SPIKE_VECTOR_BUCKET   S3 vector bucket name (``s3vectors://<bucket>/<prefix>``)
  SPIKE_TENANT          GraphRAG tenant id (hex, <=25 chars)
  GRAPHRAG_PG_STATS_FILE  JSONL of every Data API statement (timing, retries, deadlocks)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("SPIKE_VECTOR_BUCKET", "ckg-dev-graphrag-vectors-951526286029")
TENANT = os.environ.get("SPIKE_TENANT", "5b1c0de5b1c0de5b1c0de5b1c")
PREFIX = os.environ.get("SPIKE_PREFIX", "spike")
WORK = Path(os.environ.get("SPIKE_WORKDIR", "C:/Github/spike-work"))
SCHEMA = os.environ.get("SPIKE_SCHEMA", "lg")
GRAPH_URI = f"aurora-pg://?schema={SCHEMA}"
VECTOR_URI = f"s3vectors://{BUCKET}/{PREFIX}"
LLM_MODEL = os.environ.get("SPIKE_LLM", "us.anthropic.claude-sonnet-4-6")
EMBED_MODEL = "us.cohere.embed-v4:0"
INDEXES = ["chunk", "topic", "statement"]


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _pdf_text(path: Path) -> str:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    return "\n\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))


def load_documents(paths: list[Path]):
    from graphrag_toolkit.lexical_graph import add_versioning_info
    from llama_index.core import Document

    docs = []
    for p in paths:
        text = _docx_text(p) if p.suffix == ".docx" else _pdf_text(p)
        meta = {"filename": p.name, "source_s3_key": f"spike/{p.parent.parent.name}/{p.name}"}
        meta = add_versioning_info(meta, id_fields=["source_s3_key"])
        d = Document(text=text, metadata=meta)
        d.excluded_llm_metadata_keys = list(meta.keys())
        d.excluded_embed_metadata_keys = list(meta.keys())
        docs.append(d)
        print(f"loaded {p.name}: {len(text)} chars", flush=True)
    return docs


def configure(workers_build: int, batch_write_size: int | None):
    from botocore.config import Config
    from spike_embed import ThrottledBatchEmbedding
    from graphrag_toolkit.lexical_graph import GraphRAGConfig
    from llama_index.llms.bedrock_converse import BedrockConverse

    GraphRAGConfig.aws_region = REGION
    GraphRAGConfig.enable_versioning = True
    GraphRAGConfig.embed_model = ThrottledBatchEmbedding(
        embed_model_id=EMBED_MODEL, embed_dimensions=1024, embed_region=REGION
    )
    GraphRAGConfig.embed_dimensions = 1024
    GraphRAGConfig.extraction_llm = BedrockConverse(
        model=LLM_MODEL,
        temperature=0.0,
        max_tokens=16384,
        timeout=300,
        max_retries=10,
        region_name=REGION,
        botocore_config=Config(read_timeout=300, retries={"max_attempts": 10, "mode": "standard"}),
    )
    GraphRAGConfig.extraction_num_workers = 1
    GraphRAGConfig.extraction_num_threads_per_worker = 2
    GraphRAGConfig.build_num_workers = workers_build
    if batch_write_size:
        GraphRAGConfig.build_batch_write_size = batch_write_size
    GraphRAGConfig.local_output_dir = str(WORK / "output")


def indexing_config():
    from graphrag_toolkit.lexical_graph import ExtractionConfig, IndexingConfig
    from graphrag_toolkit.lexical_graph.indexing.extract import InferClassificationsConfig

    return IndexingConfig(
        extraction=ExtractionConfig(
            enable_proposition_extraction=True,
            preferred_entity_classifications=[],
            infer_entity_classifications=InferClassificationsConfig(replace_default_classifications=True),
            preferred_topics=[],
        )
    )


def open_stores():
    from coa_common.graphrag_pg.store import register
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory

    register()
    return (
        GraphStoreFactory.for_graph_store(GRAPH_URI),
        VectorStoreFactory.for_vector_store(VECTOR_URI, index_names=INDEXES),
    )


def cmd_schema(_a):
    from coa_common.aurora_data_api.client import build_client_from_env
    from coa_common.graphrag_pg.schema import ensure_schema

    ensure_schema(build_client_from_env(), SCHEMA)
    print("schema ready")


def _index(workers: int, bws: int | None):
    from graphrag_toolkit.lexical_graph import LexicalGraphIndex

    configure(workers, bws)
    g, v = open_stores()
    return LexicalGraphIndex(g, v, tenant_id=TENANT, indexing_config=indexing_config())


def cmd_extract(a):
    from graphrag_toolkit.lexical_graph.indexing.load import FileBasedDocs

    idx = _index(1, None)
    docs = load_documents([Path(p) for p in a.docs])
    handler = FileBasedDocs(
        docs_directory=str(WORK / "extracted"),
        collection_id=a.collection,
        filename_sanitizer=lambda s: s.replace(":", "_"),
    )
    t0 = time.time()
    idx.extract(docs, handler=handler)
    print(f"EXTRACT_SECONDS {time.time() - t0:.1f}")


def cmd_build(a):
    from graphrag_toolkit.lexical_graph.indexing.load import FileBasedDocs

    idx = _index(a.workers, a.batch_write_size)
    handler = FileBasedDocs(
        docs_directory=str(WORK / "extracted"),
        collection_id=a.collection,
        filename_sanitizer=lambda s: s.replace(":", "_"),
    )
    t0 = time.time()
    idx.build(handler)
    print(f"BUILD_SECONDS {time.time() - t0:.1f} workers={a.workers}")


def _patch_topic_beam_for_s3_vectors() -> None:
    """TopicBeamSearch 3.20.0 seeds through ``topic_index.client.query(...)``, an OpenSearch client call.

    ``S3VectorIndex.client`` is the boto3 s3vectors client, which has no ``query``. Seed through the
    backend-neutral ``VectorIndex.top_k`` instead. Spike-only; the production fix belongs in COA's
    strategy builder (a TopicBeamSearch subclass).
    """
    import numpy as np
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch
    from graphrag_toolkit.lexical_graph.storage.vector.vector_index import to_embedded_query

    def get_start_node_ids(self, query_bundle):
        self._topic_cache.clear()
        topic_index = self.vector_store.get_index("topic")
        query_bundle = to_embedded_query(query_bundle, topic_index.embed_model)
        self._query_embedding = np.array(query_bundle.embedding)
        results = topic_index.top_k(query_bundle, top_k=self.top_k)
        return [r["topic"]["topicId"] for r in results if (r.get("topic") or {}).get("topicId")]

    TopicBeamSearch.get_start_node_ids = get_start_node_ids


def cmd_query(a):
    from coa_serve.lexical.strategies import _build_topic_beam_engine

    configure(1, None)
    _patch_topic_beam_for_s3_vectors()
    g, v = open_stores()
    engine = _build_topic_beam_engine(g, v, tenant_id=TENANT)
    out = []
    questions = [q.strip() for q in Path(a.questions).read_text(encoding="utf-8").splitlines() if q.strip()]
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        try:
            nodes = engine.retrieve(q)
            err = ""
        except Exception as exc:  # noqa: BLE001
            nodes, err = [], f"{type(exc).__name__}: {exc}"[:600]
        dt = time.time() - t0
        rec = {"i": i, "question": q, "seconds": round(dt, 2), "error": err, "results": []}
        for n in nodes:
            res = n.node.metadata.get("result", {})
            topics = res.get("topics", [])
            stmts = [s.get("statement") for t in topics for s in t.get("statements", [])]
            rec["results"].append(
                {
                    "score": n.score,
                    "source": (res.get("source", {}).get("metadata", {}) or {}).get("filename"),
                    "topics": [t.get("topic") for t in topics][:6],
                    "n_statements": len(stmts),
                    "top_statements": stmts[:3],
                }
            )
        out.append(rec)
        print(f"Q{i} {dt:.1f}s results={len(nodes)} err={err[:80]}", flush=True)
    Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")


def cmd_counts(_a):
    from coa_common.aurora_data_api.client import build_client_from_env

    c = build_client_from_env()
    print(json.dumps(c.execute(f"SELECT label, count(*) AS n FROM {SCHEMA}.node GROUP BY 1 ORDER BY 1"), indent=1))
    print(json.dumps(c.execute(f"SELECT type, count(*) AS n FROM {SCHEMA}.edge GROUP BY 1 ORDER BY 1"), indent=1))
    print(
        c.execute(
            f"SELECT pg_size_pretty(pg_total_relation_size('{SCHEMA}.node')) AS node, pg_size_pretty(pg_total_relation_size('{SCHEMA}.edge')) AS edge"
        )
    )


def cmd_wipe(_a):
    from coa_common.aurora_data_api.client import build_client_from_env

    c = build_client_from_env()
    c.execute(f"TRUNCATE {SCHEMA}.node, {SCHEMA}.edge")
    print("truncated")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema")
    e = sub.add_parser("extract")
    e.add_argument("docs", nargs="+")
    e.add_argument("--collection", default="spike1")
    b = sub.add_parser("build")
    b.add_argument("--collection", default="spike1")
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--batch-write-size", type=int, default=None)
    q = sub.add_parser("query")
    q.add_argument("questions")
    q.add_argument("--out", default=str(WORK / "query-results.json"))
    sub.add_parser("counts")
    sub.add_parser("wipe")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    {
        "schema": cmd_schema,
        "extract": cmd_extract,
        "build": cmd_build,
        "query": cmd_query,
        "counts": cmd_counts,
        "wipe": cmd_wipe,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
