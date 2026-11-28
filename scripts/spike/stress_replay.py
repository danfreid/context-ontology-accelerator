# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Write-path stress for the Aurora graph store.

Replays the graph write queries captured from a real build (``GRAPHRAG_PG_CAPTURE``) K times, rewriting
ids so each copy is a new set of documents. A fraction of the entity ids stays shared across copies so
workers contend on the same entity rows, and the ``__SYS_Class__`` counters stay shared. Copies are
spread over T threads (the build's workers) and every statement goes through ``AuroraGraphStore``.

Usage: stress_replay.py CAPTURE.jsonl --copies 55 --threads 4 --rows-per-stmt 25 --schema lgstress
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from coa_common.graphrag_pg.schema import ensure_schema
from coa_common.graphrag_pg.store import AuroraGraphStore, UnsupportedGraphQueryError, parse_write

SUFFIX_KEYS = {
    "sourceId",
    "source_id",
    "chunk_id",
    "target_id",
    "chunkId",
    "statement_id",
    "prev_statement_id",
    "topic_id",
    "fact_id",
    "__source_hash__",
}
ENTITY_KEYS = {"e_id", "s_id", "o_id", "entity_id", "entityId"}


def remap(obj, copy: int, shared_frac: float):
    suffix = f"~c{copy}"
    if isinstance(obj, list):
        return [remap(x, copy, shared_frac) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SUFFIX_KEYS and isinstance(v, str):
                out[k] = v + suffix
            elif k in ENTITY_KEYS and isinstance(v, str):
                h = int(hashlib.md5(v.encode()).hexdigest()[:6], 16) / 0xFFFFFF
                out[k] = v if h < shared_frac else v + suffix
            elif k == "text" and isinstance(v, str):
                out[k] = v
            else:
                out[k] = remap(v, copy, shared_frac)
        return out
    return obj


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--copies", type=int, default=55)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--rows-per-stmt", type=int, default=25)
    ap.add_argument("--schema", default="lgstress")
    ap.add_argument("--shared-frac", type=float, default=0.3)
    a = ap.parse_args()

    store = AuroraGraphStore(schema_name=a.schema)
    ensure_schema(store.client, a.schema)

    # group captured writes by query text, keep first-seen order
    queries: dict[str, list[dict]] = {}
    skipped = 0
    import glob

    lines = [ln for p in sorted(glob.glob(a.capture + "*")) for ln in open(p, encoding="utf-8")]
    for line in lines:
        rec = json.loads(line)
        cy, params = rec["cypher"], rec["params"]
        rows = params.get("params") if isinstance(params, dict) else None
        if not rows:
            skipped += 1
            continue
        try:
            if "// insert graph summary" not in cy:
                parse_write(cy, rows[:1])
        except UnsupportedGraphQueryError:
            skipped += 1
            continue
        queries.setdefault(cy, []).extend(rows)
    print(
        f"write query shapes={len(queries)} captured_rows={sum(len(v) for v in queries.values())} skipped_nonwrites={skipped}"
    )

    lock = threading.Lock()
    stats = defaultdict(float)

    def run_copy(copy: int) -> None:
        for cy, rows in queries.items():
            mapped = remap(rows, copy, a.shared_frac)
            for i in range(0, len(mapped), a.rows_per_stmt):
                t0 = time.perf_counter()
                store.execute_query_with_retry(
                    cy, {"params": mapped[i : i + a.rows_per_stmt]}, max_attempts=5, max_wait=3
                )
                dt = time.perf_counter() - t0
                with lock:
                    stats["stmts"] += 1
                    stats["sum_s"] += dt
                    stats["max_s"] = max(stats["max_s"], dt)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        list(ex.map(run_copy, range(a.copies)))
    wall = time.time() - t0
    print(f"WRITE_WALL_SECONDS {wall:.1f} copies={a.copies} threads={a.threads} rows_per_stmt={a.rows_per_stmt}")
    print({k: round(v, 2) for k, v in stats.items()})
    for r in store.client.execute(f"SELECT label, count(*) AS n FROM {a.schema}.node GROUP BY 1 ORDER BY 1"):
        print(r)
    print(store.client.execute(f"SELECT type, count(*) AS n FROM {a.schema}.edge GROUP BY 1 ORDER BY 1"))
    print(
        store.client.execute(
            f"SELECT pg_size_pretty(pg_total_relation_size('{a.schema}.node')) AS node, pg_size_pretty(pg_total_relation_size('{a.schema}.edge')) AS edge"
        )
    )


if __name__ == "__main__":
    main()
