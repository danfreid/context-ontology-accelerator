# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""graphrag-lexical-graph ``GraphStore`` backed by Aurora PostgreSQL through the RDS Data API.

The toolkit hands a ``GraphStore`` openCypher text plus a parameter dict. This store has no Cypher
engine. It recognises each query the toolkit emits and runs SQL for it:

* **Writes** (``UNWIND $params AS params MERGE ...``) are parsed by a small interpreter. The grammar is
  exactly what the toolkit builders emit: ``MERGE (v:`Label`{idKey: params.field})``, ``ON CREATE/MATCH
  SET v.prop = params.field``, ``MERGE (a)-[:`TYPE`{value: params.p}]->(b)`` and one nested ``UNWIND``.
* **Reads** are matched by fingerprint and answered by hand-written SQL.

An unrecognised query raises :class:`UnsupportedGraphQueryError` (fail closed) and is not retried.

Register with ``GraphStoreFactory.register(AuroraGraphStoreFactory)`` and open with
``aurora-pg://?schema=lg``. Connection settings come from ``AURORA_*`` (see ``AuroraConfig``).
"""

from __future__ import annotations

# ruff: noqa: E501
import json
import logging
import os
import random
import re
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from graphrag_toolkit.lexical_graph.storage.constants import LEXICAL_GRAPH_LABELS
from graphrag_toolkit.lexical_graph.storage.graph import GraphStore, GraphStoreFactoryMethod, get_log_formatting
from pydantic import PrivateAttr

from coa_common.aurora_data_api.client import AuroraDataApiClient, build_client_from_env, json_text
from coa_common.aurora_data_api.errors import AuroraQueryError, ResponseTooLargeError
from coa_common.graphrag_pg.schema import _SCHEMA_RE

log = logging.getLogger(__name__)

SCHEME = "aurora-pg://"
JSON_CHUNK_BYTES = 1_200_000
_DEADLOCK = re.compile(r"deadlock detected|could not serialize access|40P01|40001", re.IGNORECASE)
_BASE_LABELS = sorted(LEXICAL_GRAPH_LABELS, key=len, reverse=True)
_ID_KEY = {
    "__Source__": "sourceId",
    "__Chunk__": "chunkId",
    "__Topic__": "topicId",
    "__Statement__": "statementId",
    "__Fact__": "factId",
    "__Entity__": "entityId",
    "__SYS_Class__": "sysClassId",
}
_COLUMN_PROPS = ("value", "search_str", "class")


class UnsupportedGraphQueryError(Exception):
    """The toolkit sent a Cypher shape this store has no SQL for."""


def split_label(label: str) -> tuple[str, str] | None:
    """``__Entity__abc__`` -> (``__Entity__``, ``abc``); ``__Entity__`` -> (``__Entity__``, ``''``); else None."""
    for base in _BASE_LABELS:
        if label == base:
            return base, ""
        if label.startswith(base) and label.endswith("__") and len(label) > len(base) + 2:
            return base, label[len(base) : -2]
    return None


_LABEL_TOKEN = re.compile(r"`(__[^`]+__)`")


def canonicalize(cypher: str) -> tuple[str, str]:
    """Return ``(cypher with tenant label suffixes removed, tenant)``. Tenant is ``''`` for the default."""
    tenant = ""

    def repl(m: re.Match[str]) -> str:
        nonlocal tenant
        parts = split_label(m.group(1))
        if parts is None:
            return m.group(0)
        if parts[1]:
            tenant = parts[1]
        return f"`{parts[0]}`"

    return _LABEL_TOKEN.sub(repl, cypher), tenant


def _unq(name: str) -> str:
    return name[1:-1].replace("``", "`") if name.startswith("`") else name


# ── write grammar ────────────────────────────────────────────────────────────
_MERGE_NODE = re.compile(r"MERGE\s*\((\w+):`([^`]+)`\s*\{\s*(\w+)\s*:\s*(\w+)\.(\w+)\s*\}\s*\)")
_MERGE_EDGE = re.compile(
    r"MERGE\s*\((\w+)\)-\[\s*(\w*)\s*:`([^`]+)`\s*(?:\{\s*value\s*:\s*params\.(\w+)\s*\})?\s*\]->\((\w+)\)"
)
_ASSIGN = re.compile(r"(\w+)\.(`(?:[^`]|``)*`|\w+)\s*=\s*params\.(`(?:[^`]|``)*`|\w+)")
_NESTED = re.compile(r"UNWIND\s+params\.(\w+)\s+as\s+(\w+)", re.IGNORECASE)
_HASH_GUARD = re.compile(
    r"WITH\s+(\w+),\s*params,\s*\1\.(\w+)\s+AS\s+owner\s+WHERE\s+owner IS NULL OR owner = params\.`([^`]+)`"
)
_HASH_SET = re.compile(r"SET\s+(\w+)\.(\w+)\s*=\s*coalesce\(owner,\s*params\.`([^`]+)`\)")


@dataclass
class _NodeRec:
    """One node to upsert."""

    id: str
    label: str
    tenant: str
    props: dict[str, Any] = field(default_factory=dict)
    real: bool = False  # carries at least one assigned property


@dataclass
class WritePlan:
    """Nodes and edges one batched write query asks for."""

    nodes: list[_NodeRec]
    edges: list[tuple[str, str, str, str]]  # (type, src, dst, value)
    hash_guard: str | None = None


def parse_write(cypher: str, rows: list[dict[str, Any]]) -> WritePlan:
    """Interpret one batched write query over its ``params`` rows."""
    text, _ = canonicalize(cypher)
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))
    if not re.search(r"UNWIND\s+\$params\s+AS\s+params", body) or not _MERGE_NODE.search(body):
        raise UnsupportedGraphQueryError(f"not a recognised write: {body[:300]!r}")
    if re.search(r"\bSET\s+\w+\s*:`", body) or "DELETE" in body or "MATCH" in re.sub(r"ON MATCH", "", body):
        raise UnsupportedGraphQueryError(f"unsupported write clause: {body[:300]!r}")
    merges = list(_MERGE_NODE.finditer(body))
    nested = _NESTED.search(body)
    guard = _HASH_GUARD.search(body)
    hash_set = _HASH_SET.search(body)
    # Assignments, grouped by variable. ON CREATE and ON MATCH carry identical lists in every builder.
    assigns: dict[str, dict[str, str]] = {}
    for m in _ASSIGN.finditer(body):
        assigns.setdefault(m.group(1), {})[_unq(m.group(2))] = _unq(m.group(3))
    if hash_set:
        assigns.setdefault(hash_set.group(1), {})[hash_set.group(2)] = hash_set.group(3)
    edges_tpl = list(_MERGE_EDGE.finditer(body))
    # Merges raw labels (with tenant) were canonicalised above, so recover tenant per label from the original.
    raw_labels = {m.group(1): m.group(2) for m in _MERGE_NODE.finditer(cypher)}

    nodes: dict[str, _NodeRec] = {}
    edges: dict[tuple[str, str, str, str], None] = {}

    def run(env: dict[str, dict[str, Any]]) -> None:
        ids: dict[str, str] = {}
        for m in merges:
            var, _label, _key, src, field_ = m.groups()
            rid = env[src].get(field_)
            if rid is None:
                return
            raw = raw_labels.get(var, m.group(2))
            parts = split_label(raw) or (raw, "")
            rec = nodes.get(rid) or _NodeRec(id=rid, label=parts[0], tenant=parts[1])
            nodes[rid] = rec
            ids[var] = rid
            for prop, fld in assigns.get(var, {}).items():
                v = env["params"].get(fld)
                if v is not None:
                    rec.props[prop] = v
                    rec.real = True
        for e in edges_tpl:
            svar, _rvar, etype, vfield, dvar = e.groups()
            val = env["params"].get(vfield) if vfield else ""
            edges[(etype, ids[svar], ids[dvar], "" if val is None else str(val))] = None

    for row in rows:
        if nested:
            for item in row.get(nested.group(1)) or []:
                run({"params": row, nested.group(2): item})
        else:
            run({"params": row})
    return WritePlan(
        nodes=list(nodes.values()),
        edges=list(edges),
        hash_guard=guard.group(2) if guard else None,
    )


def _node_json(rec: _NodeRec) -> dict[str, Any]:
    props = dict(rec.props)
    out: dict[str, Any] = {"id": rec.id, "label": rec.label, "tenant": rec.tenant}
    if rec.label != "__Source__":
        for c in _COLUMN_PROPS:
            if c in props:
                v = props.pop(c)
                out[c] = None if v is None else str(v)
    out["props"] = props
    return out


# ── store ────────────────────────────────────────────────────────────────────
class AuroraGraphStore(GraphStore):
    """GraphStore over Aurora PostgreSQL (RDS Data API). Not a Cypher engine; see the module docstring."""

    schema_name: str = "lg"
    stats_file: str | None = None
    _client: AuroraDataApiClient | None = PrivateAttr(default=None)
    _lock: Any = PrivateAttr(default_factory=threading.Lock)

    def __getstate__(self) -> dict[str, Any]:
        """Drop the boto client and lock so the store pickles into build worker processes."""
        state = super().__getstate__()
        priv = dict(state.get("__pydantic_private__") or {})
        priv["_client"] = None
        priv.pop("_lock", None)
        state["__pydantic_private__"] = priv
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore the store in a worker; the client is rebuilt lazily."""
        super().__setstate__(state)
        priv = self.__pydantic_private__ or {}
        priv["_lock"] = threading.Lock()
        self.__pydantic_private__ = priv

    @property
    def client(self) -> AuroraDataApiClient:
        """The Data API client (built from ``AURORA_*`` on first use)."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = build_client_from_env()
        return self._client

    def unretriable_exception_types(self) -> tuple[type[BaseException], ...]:
        """An unrecognised query will never succeed on retry."""
        return (UnsupportedGraphQueryError,)

    # ── SQL execution with deadlock retry and per-statement stats ────────────
    def _q(self, kind: str, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        attempts = 0
        while True:
            t0 = time.perf_counter()
            err = ""
            try:
                return self.client.execute(sql, params)
            except AuroraQueryError as exc:
                err = str(exc)[:200]
                if _DEADLOCK.search(str(exc)) and attempts < 6:
                    attempts += 1
                    time.sleep(random.uniform(0.05, 0.4) * attempts)
                    continue
                raise
            finally:
                self._stat(kind, (time.perf_counter() - t0) * 1000, attempts, err)

    def _stat(self, kind: str, ms: float, retries: int, err: str) -> None:
        path = self.stats_file or os.environ.get("GRAPHRAG_PG_STATS_FILE")
        if not path:
            return
        rec = {"t": round(time.time(), 3), "pid": os.getpid(), "kind": kind, "ms": round(ms, 1), "retries": retries}
        if err:
            rec["err"] = err
            rec["deadlock"] = bool(_DEADLOCK.search(err))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))  # one write call: atomic across processes
        finally:
            os.close(fd)

    @property
    def _s(self) -> str:
        if not _SCHEMA_RE.match(self.schema_name):
            raise ValueError("invalid schema")
        return self.schema_name

    # ── GraphStore ────────────────────────────────────────────────────────────
    def _execute_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, correlation_id: str | None = None
    ) -> list[Any]:
        parameters = parameters or {}
        capture = os.environ.get("GRAPHRAG_PG_CAPTURE")
        if capture:
            fd = os.open(f"{capture}.{os.getpid()}", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                os.write(fd, (json.dumps({"cypher": cypher, "params": parameters}, default=str) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        canon, tenant = canonicalize(cypher)
        flat = " ".join(canon.split())
        for _name, pred, handler in _READ_HANDLERS:
            if pred(flat):
                return handler(self, flat, tenant, parameters)
        if "params" in parameters and re.search(r"UNWIND\s+\$params\s+AS\s+params", canon):
            return self._write(canon, cypher, tenant, parameters["params"])
        raise UnsupportedGraphQueryError(f"no SQL for query: {flat[:600]}")

    # ── writes ────────────────────────────────────────────────────────────────
    def _write(self, canon: str, cypher: str, tenant: str, rows: list[dict[str, Any]]) -> list[Any]:
        if not rows:
            return []
        if "// insert graph summary" in canon:
            return self._write_summary(rows, tenant)
        plan = parse_write(cypher, rows)
        real = [_node_json(n) for n in plan.nodes if n.real]
        stub = [_node_json(n) for n in plan.nodes if not n.real]
        real.sort(key=lambda r: r["id"])
        stub.sort(key=lambda r: r["id"])
        edges = [{"type": t, "src": s, "dst": d, "value": v} for (t, s, d, v) in sorted(plan.edges)]
        guard = ""
        if plan.hash_guard:
            g = plan.hash_guard
            guard = f"WHERE t.props->>'{g}' IS NULL OR t.props->>'{g}' = EXCLUDED.props->>'{g}'"
        sql = _WRITE_SQL.format(s=self._s, guard=guard)
        # One statement per payload slice; node slices go first, then edges.
        for nodes_chunk, stub_chunk, edge_chunk in _slices(real, stub, edges):
            self._q("write", sql, {"real": nodes_chunk, "stub": stub_chunk, "edges": edge_chunk})
        return []

    def _write_summary(self, rows: list[dict[str, Any]], tenant: str) -> list[Any]:
        """``__SYS_Class__`` counters. Rows are aggregated first so a hot class gets one update per statement."""
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str, str], int] = {}
        for r in rows:
            sc, oc = r["sc_id"], r["oc_id"]
            inc = 2 if sc == oc else 1
            for nid, val in ((sc, r["sc"]), (oc, r["oc"])) if sc != oc else ((sc, r["sc"]),):
                n = nodes.setdefault(
                    nid, {"id": nid, "label": "__SYS_Class__", "tenant": tenant, "value": val, "inc": 0}
                )
                n["inc"] += inc
            k = ("__SYS_RELATION__", sc, oc, str(r["p"]))
            edges[k] = edges.get(k, 0) + inc
        node_rows = sorted(nodes.values(), key=lambda n: n["id"])
        edge_rows = [{"type": t, "src": s, "dst": d, "value": v, "inc": c} for (t, s, d, v), c in sorted(edges.items())]
        self._q(
            "summary", _SUMMARY_SQL.format(s=self._s), {"nodes": json_text(node_rows), "edges": json_text(edge_rows)}
        )
        return []


def _slices(real: list, stub: list, edges: list) -> Iterator[tuple[str, str, str]]:
    """Yield JSON-string triples whose sizes stay under the Data API request limit."""
    size = (
        sum(len(json_text(x)) for x in real)
        + sum(len(json_text(x)) for x in stub)
        + sum(len(json_text(x)) for x in edges)
    )
    if size <= JSON_CHUNK_BYTES:
        yield json_text(real), json_text(stub), json_text(edges)
        return
    for group in (real, stub, edges):
        for part in _json_parts(group):
            if group is real:
                yield part, "[]", "[]"
            elif group is stub:
                yield "[]", part, "[]"
            else:
                yield "[]", "[]", part


def _json_parts(rows: Iterable[Any]) -> Iterator[str]:
    from coa_common.aurora_data_api.client import bulk_json_chunks

    yield from bulk_json_chunks(rows, JSON_CHUNK_BYTES)


_WRITE_SQL = """
WITH real AS (
  INSERT INTO {s}.node AS t (id, label, tenant, value, search_str, class, props)
  SELECT x.id, x.label, x.tenant, x.value, x.search_str, x.class, COALESCE(x.props, '{{}}'::jsonb)
  FROM jsonb_to_recordset(CAST(:real AS jsonb))
       AS x(id text, label text, tenant text, value text, search_str text, class text, props jsonb)
  ORDER BY x.id
  ON CONFLICT (id) DO UPDATE SET
    value = COALESCE(EXCLUDED.value, t.value),
    search_str = COALESCE(EXCLUDED.search_str, t.search_str),
    class = COALESCE(EXCLUDED.class, t.class),
    props = t.props || EXCLUDED.props
  {guard}
  RETURNING 1
), stub AS (
  INSERT INTO {s}.node (id, label, tenant)
  SELECT x.id, x.label, x.tenant
  FROM jsonb_to_recordset(CAST(:stub AS jsonb)) AS x(id text, label text, tenant text)
  ORDER BY x.id
  ON CONFLICT (id) DO NOTHING
  RETURNING 1
)
INSERT INTO {s}.edge (type, src, dst, value)
SELECT e.type, e.src, e.dst, e.value
FROM jsonb_to_recordset(CAST(:edges AS jsonb)) AS e(type text, src text, dst text, value text)
ORDER BY e.type, e.src, e.dst, e.value
ON CONFLICT DO NOTHING
"""

_SUMMARY_SQL = """
WITH n AS (
  INSERT INTO {s}.node AS t (id, label, tenant, value, props)
  SELECT x.id, x.label, x.tenant, x.value, jsonb_build_object('count', x.inc)
  FROM jsonb_to_recordset(CAST(:nodes AS jsonb)) AS x(id text, label text, tenant text, value text, inc bigint)
  ORDER BY x.id
  ON CONFLICT (id) DO UPDATE SET
    props = jsonb_set(t.props, '{{count}}', to_jsonb(COALESCE((t.props->>'count')::bigint, 0) + (EXCLUDED.props->>'count')::bigint))
  RETURNING 1
)
INSERT INTO {s}.edge AS t (type, src, dst, value, props)
SELECT e.type, e.src, e.dst, e.value, jsonb_build_object('count', e.inc)
FROM jsonb_to_recordset(CAST(:edges AS jsonb)) AS e(type text, src text, dst text, value text, inc bigint)
ORDER BY e.type, e.src, e.dst, e.value
ON CONFLICT (type, src, dst, value) DO UPDATE SET
  props = jsonb_set(t.props, '{{count}}', to_jsonb(COALESCE((t.props->>'count')::bigint, 0) + (EXCLUDED.props->>'count')::bigint))
"""


# ── reads ────────────────────────────────────────────────────────────────────
def _ids_param(ids: Iterable[str]) -> str:
    return json_text(sorted(set(ids)))


def _batched(ids: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _q_adaptive(store: AuroraGraphStore, kind: str, sql: str, ids: list[str], size: int = 200) -> list[dict[str, Any]]:
    """Run ``sql`` (taking ``:ids``) over ``ids`` in batches, halving the batch on a 1 MiB response."""
    out: list[dict[str, Any]] = []
    queue = list(_batched(ids, size))
    while queue:
        batch = queue.pop(0)
        try:
            out.extend(store._q(kind, sql, {"ids": _ids_param(batch)}))
        except ResponseTooLargeError:
            if len(batch) == 1:
                raise
            mid = len(batch) // 2
            queue[:0] = [batch[:mid], batch[mid:]]
    return out


def _props_map(rec: dict[str, Any], label: str, with_columns: bool = True) -> dict[str, Any]:
    props = json.loads(rec["props"]) if rec.get("props") else {}
    out: dict[str, Any] = {_ID_KEY.get(label, "id"): rec["id"]}
    if with_columns:
        for c in _COLUMN_PROPS:
            if rec.get(c) is not None:
                out[c] = rec[c]
    out.update(props)
    return out


def _is_statements_by_topic(flat: str) -> bool:
    return flat.lstrip().startswith("// get statements grouped by topic and source")


def _h_statements_by_topic(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    from graphrag_toolkit.lexical_graph.versioning import (
        BUILD_TIMESTAMP,
        EXTRACT_TIMESTAMP,
        TIMESTAMP_LOWER_BOUND,
        TIMESTAMP_UPPER_BOUND,
        VALID_FROM,
        VALID_TO,
        VERSION_INDEPENDENT_ID_FIELDS,
    )

    statement_ids = list(params.get("statementIds") or [])
    limit = int(params.get("limit") or 10)
    chunk_details = "metadata: properties(c)" in flat
    if not statement_ids:
        return []
    s = store._s
    paths = _q_adaptive(
        store,
        "read:statements_by_topic",
        f"""SELECT t.id AS tid, t.value AS tvalue, l.id AS lid, l.value AS lvalue, l.props->>'details' AS details,
                   c.id AS cid, so.id AS sid
            FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id)
            JOIN {s}.node l ON l.id = q.id AND l.label = '__Statement__'
            JOIN {s}.edge bt ON bt.type = '__BELONGS_TO__' AND bt.src = l.id
            JOIN {s}.node t ON t.id = bt.dst
            JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.src = l.id
            JOIN {s}.node c ON c.id = mi.dst
            JOIN {s}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = c.id
            JOIN {s}.node so ON so.id = ef.dst""",
        statement_ids,
    )
    if not paths:
        return []
    chunk_ids = sorted({p["cid"] for p in paths})
    source_ids = sorted({p["sid"] for p in paths})
    chunks: dict[str, dict[str, Any]] = {}
    if chunk_details:
        for r in _q_adaptive(
            store,
            "read:chunk_props",
            f"SELECT id, value, search_str, class, props::text AS props FROM {s}.node n "
            f"WHERE n.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
            chunk_ids,
            size=50,
        ):
            chunks[r["id"]] = _props_map(r, "__Chunk__")
    sources: dict[str, dict[str, Any]] = {}
    for r in _q_adaptive(
        store,
        "read:source_props",
        f"SELECT id, props::text AS props FROM {s}.node n WHERE n.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
        source_ids,
    ):
        sources[r["id"]] = _props_map(r, "__Source__", with_columns=False)

    def source_map(sid: str) -> dict[str, Any]:
        m = sources.get(sid, {"sourceId": sid})
        return {
            "sourceId": sid,
            "metadata": m,
            "versioning": {
                "valid_from": m.get(VALID_FROM, TIMESTAMP_LOWER_BOUND),
                "valid_to": m.get(VALID_TO, TIMESTAMP_UPPER_BOUND),
                "extract_timestamp": m.get(EXTRACT_TIMESTAMP, TIMESTAMP_LOWER_BOUND),
                "build_timestamp": m.get(BUILD_TIMESTAMP, TIMESTAMP_LOWER_BOUND),
                "id_fields": str(m.get(VERSION_INDEPENDENT_ID_FIELDS) or "").split(";"),
            },
        }

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for p in sorted(paths, key=lambda r: (r["sid"], r["tid"], r["lid"], r["cid"])):
        g = grouped.setdefault(
            (p["sid"], p["tid"]), {"topic": p["tvalue"], "topicId": p["tid"], "chunks": {}, "statements": []}
        )
        g["chunks"].setdefault(
            p["cid"],
            {"chunkId": p["cid"], "value": None, "metadata": dict(chunks.get(p["cid"], {})) if chunk_details else {}},
        )
        g["statements"].append(
            {
                "statementId": p["lid"],
                "statement": p["lvalue"],
                "facts": [],
                "details": p["details"],
                "chunkId": p["cid"],
                "score": 0,
            }
        )
    by_source: dict[str, list[dict[str, Any]]] = {}
    for (sid, _tid), g in grouped.items():
        topic = {**g, "chunks": list(g["chunks"].values())}
        by_source.setdefault(sid, []).append(topic)
    results = []
    for sid, topics in by_source.items():
        score = sum(len(t["statements"]) // len(t["chunks"]) for t in topics)
        results.append({"result": {"score": score, "source": source_map(sid), "topics": topics}})
    results.sort(key=lambda r: (-r["result"]["score"], r["result"]["source"]["sourceId"]))
    return results[:limit]


def _h_facts(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("statementIds") or [])
    if not ids:
        return []
    s = store._s
    rows = _q_adaptive(
        store,
        "read:facts",
        f"""SELECT DISTINCT e.dst AS "statementId", f.value AS value
            FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id)
            JOIN {s}.edge e ON e.type = '__SUPPORTS__' AND e.dst = q.id
            JOIN {s}.node f ON f.id = e.src
            ORDER BY 1, 2""",
        ids,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        if r["value"] is not None:
            out.setdefault(r["statementId"], []).append(r["value"])
    return [{"statementId": k, "facts": v} for k, v in out.items()]


def _is_expand_topics(flat: str) -> bool:
    return flat.startswith(
        "MATCH (s:`__Statement__`)-[:`__BELONGS_TO__`]->(t:`__Topic__`) WHERE t.topicId IN $topicIds RETURN t.topicId AS topicId"
    )


def _h_expand_topics(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("topicIds") or [])
    if not ids:
        return []
    s = store._s
    return _q_adaptive(
        store,
        "read:topic_statements",
        f"""SELECT e.dst AS "topicId", e.src AS "statementId"
            FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id)
            JOIN {s}.edge e ON e.type = '__BELONGS_TO__' AND e.dst = q.id
            ORDER BY e.dst, e.src""",
        ids,
    )


def _is_topic_neighbors(flat: str) -> bool:
    return flat.startswith("MATCH (t:`__Topic__`) WHERE t.topicId IN $topicIds") and "AS neighborIds" in flat


def _h_topic_neighbors(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("topicIds") or [])
    if not ids:
        return []
    s = store._s
    use_entity = "COLLECT(DISTINCT e) AS entities" in flat
    use_chunk = "AS chunk_neighbors" in flat and "[] AS chunk_neighbors" not in flat
    use_adj = "AS adjacent_neighbors" in flat and "[] AS adjacent_neighbors" not in flat
    limits = [int(x) for x in re.findall(r"\)\[\.\.(\d+)\]", flat)]
    li = iter(limits)
    ent_n = next(li) if use_entity else 0
    chunk_n = next(li) if use_chunk else 0
    adj_n = next(li) if use_adj else 0
    arms = ["SELECT id AS tid, NULL::text AS nid FROM tin"]
    if use_entity:
        arms.append(
            f"""SELECT tid, nt FROM (
                  SELECT tid, nt, row_number() OVER (PARTITION BY tid ORDER BY strength DESC, nt) AS rn FROM (
                    SELECT en.tid, bt2.dst AS nt, count(*) AS strength
                    FROM (SELECT DISTINCT bt.dst AS tid, sj.src AS eid
                          FROM tin
                          JOIN {s}.edge bt ON bt.type = '__BELONGS_TO__' AND bt.dst = tin.id
                          JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = bt.src
                          JOIN {s}.edge sj ON sj.type IN ('__SUBJECT__', '__OBJECT__') AND sj.dst = su.src) en
                    JOIN {s}.edge sj2 ON sj2.type IN ('__SUBJECT__', '__OBJECT__') AND sj2.src = en.eid
                    JOIN {s}.edge su2 ON su2.type = '__SUPPORTS__' AND su2.src = sj2.dst
                    JOIN {s}.edge bt2 ON bt2.type = '__BELONGS_TO__' AND bt2.src = su2.dst
                    WHERE bt2.dst <> en.tid
                    GROUP BY en.tid, bt2.dst) x) y
                WHERE rn <= {ent_n}"""
        )
    if use_chunk:
        arms.append(
            f"""SELECT tid, nt FROM (
                  SELECT tin.id AS tid, e2.src AS nt, row_number() OVER (PARTITION BY tin.id ORDER BY e2.src) AS rn
                  FROM tin
                  JOIN {s}.edge e1 ON e1.type = '__MENTIONED_IN__' AND e1.src = tin.id
                  JOIN {s}.edge e2 ON e2.type = '__MENTIONED_IN__' AND e2.dst = e1.dst AND e2.src <> tin.id
                  JOIN {s}.node ct ON ct.id = e2.src AND ct.label = '__Topic__'
                  GROUP BY tin.id, e2.src) y
                WHERE rn <= {chunk_n}"""
        )
    if use_adj:
        arms.append(
            f"""SELECT tid, nt FROM (
                  SELECT tin.id AS tid, e2.src AS nt, row_number() OVER (PARTITION BY tin.id ORDER BY e2.src) AS rn
                  FROM tin
                  JOIN {s}.edge e1 ON e1.type = '__MENTIONED_IN__' AND e1.src = tin.id
                  JOIN {s}.edge nx ON nx.type = '__NEXT__' AND nx.src = e1.dst
                  JOIN {s}.edge e2 ON e2.type = '__MENTIONED_IN__' AND e2.dst = nx.dst AND e2.src <> tin.id
                  JOIN {s}.node at ON at.id = e2.src AND at.label = '__Topic__'
                  GROUP BY tin.id, e2.src) y
                WHERE rn <= {adj_n}"""
        )
    sql = (
        f"WITH tin AS (SELECT t.id FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id) "
        f"JOIN {s}.node t ON t.id = q.id AND t.label = '__Topic__') " + " UNION ALL ".join(arms)
    )
    rows = store._q("read:topic_neighbors", sql, {"ids": _ids_param(ids)})
    out: dict[str, set[str]] = {}
    for r in rows:
        bucket = out.setdefault(r["tid"], set())
        if r.get("nid"):
            bucket.add(r["nid"])
    return [{"sourceId": k, "neighborIds": sorted(v)} for k, v in out.items()]


def _is_width_read(flat: str) -> bool:
    return flat.startswith(
        "MATCH (c:`__SYS_Config__`) WHERE c.sourceIdWidth IS NOT NULL RETURN c.sourceIdWidth AS width"
    )


def _h_width_read(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    rows = store._q(
        "read:source_id_width",
        f"SELECT (props->'sourceIdWidth')::text AS width FROM {store._s}.node "
        "WHERE label = '__SYS_Config__' AND tenant = :tenant AND props ? 'sourceIdWidth'",
        {"tenant": tenant},
    )
    return [{"width": json.loads(r["width"])} for r in rows]


def _is_width_write(flat: str) -> bool:
    return flat.startswith("MERGE (c:`__SYS_Config__`{") and "ON CREATE SET c.sourceIdWidth = $width" in flat


def _h_width_write(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    s = store._s
    store._q(
        "write:source_id_width",
        f"INSERT INTO {s}.node (id, label, tenant, props) VALUES (:id, '__SYS_Config__', :tenant, "
        "jsonb_build_object('sourceIdWidth', CAST(:width AS jsonb))) ON CONFLICT (id) DO NOTHING",
        {"id": params["configId"], "tenant": tenant, "width": json_text(params["width"])},
    )
    rows = store._q(
        "read:source_id_width",
        f"SELECT (props->'sourceIdWidth')::text AS width FROM {s}.node WHERE id = :id",
        {"id": params["configId"]},
    )
    return [{"width": json.loads(r["width"])} for r in rows if r.get("width")]


def _is_source_sample(flat: str) -> bool:
    return flat.startswith("MATCH (s:`__Source__`) RETURN s.sourceId AS sourceId ORDER BY sourceId LIMIT")


def _h_source_sample(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    limit = int(flat.rsplit("LIMIT", 1)[1].strip())
    return store._q(
        "read:source_sample",
        f"SELECT id AS \"sourceId\" FROM {store._s}.node WHERE label = '__Source__' AND tenant = :tenant "
        "ORDER BY id LIMIT :n",
        {"tenant": tenant, "n": limit},
    )


# ── source metadata filter (Cypher WHERE over ``source.`key```) -> SQL ───────────────────────────
_F_TOKEN = re.compile(
    r"""\s*(?:
    (?P<lp>\()|(?P<rp>\))|
    (?P<kw>AND|OR|NOT)\b|
    source\.`(?P<key>(?:[^`]|``)*)`|
    (?P<op><>|<=|>=|=|<|>|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)|
    '(?P<str>(?:[^'\\]|\\.|'')*)'|
    (?P<num>-?\d+(?:\.\d+)?)
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def _tokenize_filter(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _F_TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise UnsupportedGraphQueryError(f"unsupported filter syntax near {text[pos : pos + 40]!r}")
        if m.group("key") is not None:
            toks.append(("key", m.group("key").replace("``", "`")))
        elif m.group("str") is not None:
            toks.append(("str", re.sub(r"\\(.)", r"\1", m.group("str")).replace("''", "'")))
        elif m.group("kw"):
            toks.append(("kw", m.group("kw").upper()))
        elif m.group("op"):
            toks.append(("op", " ".join(m.group("op").upper().split())))
        elif m.group("num") is not None:
            toks.append(("num", m.group("num")))
        elif m.group("lp"):
            toks.append(("lp", "("))
        else:
            toks.append(("rp", ")"))
        pos = m.end()
    return toks


def translate_source_filter(where: str, alias: str = "n") -> tuple[str, dict[str, Any]]:
    """Translate the toolkit's ``filter_config_to_opencypher_filters`` output to a SQL predicate.

    Supports AND/OR/NOT, parentheses and ``source.`key` <op> <literal>`` with ``= <> < <= > >=
    CONTAINS STARTS WITH ENDS WITH`` over string and number literals. Anything else raises
    :class:`UnsupportedGraphQueryError`. Keys and values are bound parameters, never inlined.
    """
    toks = _tokenize_filter(where)
    params: dict[str, Any] = {}
    i = 0

    def peek() -> tuple[str, str]:
        return toks[i] if i < len(toks) else ("end", "")

    def take() -> tuple[str, str]:
        nonlocal i
        t = peek()
        i += 1
        return t

    def expr() -> str:
        left = term()
        while peek() == ("kw", "OR"):
            take()
            left = f"({left} OR {term()})"
        return left

    def term() -> str:
        left = factor()
        while peek() == ("kw", "AND"):
            take()
            left = f"({left} AND {factor()})"
        return left

    def factor() -> str:
        k, v = peek()
        if (k, v) == ("kw", "NOT"):
            take()
            return f"(NOT {factor()})"
        if k == "lp":
            take()
            inner = expr()
            if take()[0] != "rp":
                raise UnsupportedGraphQueryError("unbalanced filter")
            return inner
        return atom()

    def atom() -> str:
        k, key = take()
        if k != "key":
            raise UnsupportedGraphQueryError("filter atom must start with a source property")
        ok, op = take()
        vk, val = take()
        if ok != "op" or vk not in ("str", "num"):
            raise UnsupportedGraphQueryError("unsupported filter comparison")
        n = len(params) // 2
        kp, vp = f"fk{n}", f"fv{n}"
        params[kp] = key
        compare = ("=", "<>", "<", "<=", ">", ">=")
        if vk == "num":
            if op not in compare:
                raise UnsupportedGraphQueryError(f"unsupported numeric operator {op}")
            params[vp] = float(val)
            col = f"(CASE WHEN jsonb_typeof({alias}.props -> :{kp}) = 'number' THEN ({alias}.props ->> :{kp})::numeric END)"
            return f"{col} {op} :{vp}"
        col = f"({alias}.props ->> :{kp})"
        if op in compare:
            params[vp] = val
            return f"{col} {op} :{vp}"
        like = {"CONTAINS": "%{}%", "STARTS WITH": "{}%", "ENDS WITH": "%{}"}[op]
        params[vp] = like.format(val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        return f"{col} LIKE :{vp}"

    sql = expr()
    if i != len(toks):
        raise UnsupportedGraphQueryError("trailing tokens in filter")
    return sql, params


_VER_FIND = re.compile(
    r"^// find source nodes to be versioned MATCH \(source:`__Source__`\) WHERE (?P<where>.+) AND "
    r"coalesce\(source\.(?P<idf>\w+), '(?P<default>[^']*)'\) = \$versionIndependentIdFields "
    r"RETURN \{ source_id: source\.sourceId, valid_from: coalesce\(source\.(?P<vf>\w+), (?P<lo>-?\d+)\), "
    r"valid_to: coalesce\(source\.(?P<vt>\w+), (?P<hi>\d+)\) \} AS result ORDER BY result\.valid_from DESC$"
)


def _is_version_find(flat: str) -> bool:
    return flat.startswith("// find source nodes to be versioned")


def _h_version_find(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = _VER_FIND.match(flat)
    if not m:
        raise UnsupportedGraphQueryError(f"version find shape changed: {flat[:300]}")
    where, fparams = translate_source_filter(m.group("where"))
    sql = f"""SELECT n.id AS source_id,
        COALESCE((n.props ->> :vf)::bigint, {int(m.group("lo"))}) AS valid_from,
        COALESCE((n.props ->> :vt)::bigint, {int(m.group("hi"))}) AS valid_to
      FROM {store._s}.node n
      WHERE n.label = '__Source__' AND n.tenant = :tenant AND {where}
        AND COALESCE(n.props ->> :idf, :idf_default) = :idf_value
      ORDER BY valid_from DESC"""
    rows = store._q(
        "read:version_find",
        sql,
        {
            **fparams,
            "vf": m.group("vf"),
            "vt": m.group("vt"),
            "idf": m.group("idf"),
            "idf_default": m.group("default"),
            "idf_value": params["versionIndependentIdFields"],
            "tenant": tenant,
        },
    )
    return [
        {"result": {"source_id": r["source_id"], "valid_from": int(r["valid_from"]), "valid_to": int(r["valid_to"])}}
        for r in rows
    ]


def _is_version_ids(flat: str) -> bool:
    return flat.startswith("// get chunk ids to be versioned") or flat.startswith("// get topic ids to be versioned")


def _h_version_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    s = store._s
    ids = list(params.get("sourceIds") or [])
    if not ids:
        return []
    if flat.startswith("// get chunk ids to be versioned"):
        joins = f"JOIN {s}.edge e1 ON e1.type = '__EXTRACTED_FROM__' AND e1.dst = q.id"
        node = "e1.src"
    elif "[:`__BELONGS_TO__`]-(l)" in flat:  # the statement variant reuses the topic comment
        joins = (
            f"JOIN {s}.edge e1 ON e1.type = '__EXTRACTED_FROM__' AND e1.dst = q.id "
            f"JOIN {s}.edge e2 ON e2.type = '__MENTIONED_IN__' AND e2.dst = e1.src "
            f"JOIN {s}.edge e3 ON e3.type = '__BELONGS_TO__' AND e3.dst = e2.src"
        )
        node = "e3.src"
    elif "[:`__MENTIONED_IN__`]-(t)" in flat:
        joins = (
            f"JOIN {s}.edge e1 ON e1.type = '__EXTRACTED_FROM__' AND e1.dst = q.id "
            f"JOIN {s}.edge e2 ON e2.type = '__MENTIONED_IN__' AND e2.dst = e1.src "
            f"JOIN {s}.node t ON t.id = e2.src AND t.label = '__Topic__'"
        )
        node = "e2.src"
    else:
        raise UnsupportedGraphQueryError(f"version ids shape changed: {flat[:300]}")
    rows = _q_adaptive(
        store,
        "read:version_ids",
        f"SELECT DISTINCT q.id AS sid, {node} AS nid FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id) {joins}",
        ids,
        size=20,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["sid"], []).append(r["nid"])
    return [{"result": {"sourceId": k, "nodeIds": sorted(v)}} for k, v in out.items()]


def _is_version_set(flat: str) -> bool:
    return flat.startswith("// set version info for old source node")


def _h_version_set(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = re.search(r"SET s\.(\w+) = \$versioningTimestamp, s\.(\w+) = \$versionIndependentIdFields", flat)
    if not m:
        raise UnsupportedGraphQueryError(f"version set shape changed: {flat[:300]}")
    store._q(
        "write:version_set",
        f"UPDATE {store._s}.node SET props = props || jsonb_build_object(CAST(:k1 AS text), CAST(:v1 AS jsonb), "
        "CAST(:k2 AS text), CAST(:v2 AS jsonb)) WHERE id = :id AND label = '__Source__'",
        {
            "k1": m.group(1),
            "v1": json_text(params["versioningTimestamp"]),
            "k2": m.group(2),
            "v2": json_text(params["versionIndependentIdFields"]),
            "id": params["sourceId"],
        },
    )
    return []


# ── local entity rewrites (LocalEntityRewritesGraphBuilder query trees) ────────────────────────────
def _is_local_subject_complements(flat: str) -> bool:
    return flat.startswith("// get complements matching subject (fact.subject)")


def _h_local_subject_complements(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = re.search(r"class: '([^']+)'\}\)", flat)
    if not m or not params.get("params"):
        raise UnsupportedGraphQueryError(f"local entity query shape changed: {flat[:300]}")
    return store._q(
        "read:local_subject_complements",
        f"""SELECT DISTINCT n.id AS n_id, c.id AS c_id
            FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS p("nId" text)
            JOIN {store._s}.node n ON n.id = p."nId"
            JOIN {store._s}.node c ON c.label = '__Entity__' AND c.tenant = n.tenant
                 AND c.search_str = n.search_str AND c.class = :local
            WHERE n.class <> :local""",
        {"rows": json_text(params["params"]), "local": m.group(1)},
    )


def _is_local_complement_subjects(flat: str) -> bool:
    return flat.startswith("// get subjects matching complement (fact.complement)")


def _h_local_complement_subjects(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    if not params.get("params"):
        return []
    return store._q(
        "read:local_complement_subjects",
        f"""SELECT DISTINCT n.id AS n_id, c.id AS c_id
            FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS p("nId" text, "cId" text)
            JOIN {store._s}.node n ON n.id = p."nId"
            JOIN {store._s}.node c ON c.id = p."cId\"""",
        {"rows": json_text(params["params"])},
    )


def _is_local_copy(flat: str) -> bool:
    return flat.startswith("// copy complement relationships to subject")


def _h_local_copy(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    rows = params.get("params") or []
    if not rows:
        return []
    s = store._s
    paths = f"""FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS p(n_id text, c_id text)
            JOIN {s}.node n ON n.id = p.n_id
            JOIN {s}.edge r ON r.type = '__RELATION__' AND r.dst = p.c_id
            JOIN {s}.edge o ON o.type = '__OBJECT__' AND o.src = p.c_id"""
    store._q(
        "write:local_copy",
        f"""INSERT INTO {s}.edge (type, src, dst, value)
            SELECT x.type, x.src, x.dst, x.value FROM (
              SELECT '__RELATION__' AS type, r.src AS src, p.n_id AS dst, r.value AS value {paths}
              UNION
              SELECT '__OBJECT__', p.n_id, o.dst, '' {paths}
            ) x ORDER BY 1, 2, 3, 4 ON CONFLICT DO NOTHING""",
        {"rows": json_text(rows)},
    )
    return []


def _is_local_delete(flat: str) -> bool:
    return flat.startswith("// delete complement relationships")


def _h_local_delete(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    rows = params.get("params") or []
    if not rows:
        return []
    s = store._s
    store._q(
        "write:local_delete",
        f"""WITH hit AS (
              SELECT DISTINCT p.c_id
              FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS p(c_id text)
              JOIN {s}.edge r1 ON r1.type = '__RELATION__' AND r1.dst = p.c_id
              JOIN {s}.edge r2 ON r2.type = '__OBJECT__' AND r2.src = p.c_id
            ), de AS (
              DELETE FROM {s}.edge e USING hit WHERE e.src = hit.c_id OR e.dst = hit.c_id RETURNING 1
            )
            DELETE FROM {s}.node n USING hit WHERE n.id = hit.c_id""",
        {"rows": json_text(rows)},
    )
    return []


# ── source id collision check (source_id_collision.py) ───────────────────────────────────────
_COLLISION = re.compile(
    r"^MATCH \(s:`__Source__`\) WHERE s\.sourceId IN \$sourceIds RETURN s\.sourceId AS sourceId, "
    r"s\.(?P<hash>\w+) AS sourceHash, coalesce\((?P<names>[^)]*)\) AS name$"
)


def _is_source_collision(flat: str) -> bool:
    return flat.startswith("MATCH (s:`__Source__`) WHERE s.sourceId IN $sourceIds RETURN s.sourceId AS sourceId, s.")


def _h_source_collision(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = _COLLISION.match(flat)
    if not m:
        raise UnsupportedGraphQueryError(f"source collision shape changed: {flat[:300]}")
    keys = re.findall(r"s\.`((?:[^`]|``)*)`", m.group("names"))
    bind = {f"k{i}": k.replace("``", "`") for i, k in enumerate(keys)}
    name_sql = "COALESCE(" + ", ".join(f"n.props ->> :k{i}" for i in range(len(keys))) + ")" if keys else "NULL"
    ids = list(params.get("sourceIds") or [])
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for batch in _batched(ids, 200):
        out.extend(
            store._q(
                "read:source_collision",
                f"""SELECT n.id AS "sourceId", n.props ->> :hash AS "sourceHash", {name_sql} AS name
                    FROM {store._s}.node n
                    WHERE n.label = '__Source__' AND n.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))""",
                {"hash": m.group("hash"), "ids": _ids_param(batch), **bind},
            )
        )
    return out


# ── chunk-based search (chunk_based_semantic, chunk_based) ───────────────────
_SPO = "('__SUBJECT__', '__OBJECT__')"


def _limit(params: dict[str, Any], key: str, default: int) -> int:
    return max(0, int(params.get(key) if params.get(key) is not None else default))


def _is_chunk_statements(flat: str) -> bool:
    return flat.startswith(("// chunk-based semantic graph search", "// chunk-based graph search"))


def _h_chunk_statements(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Statements of the topics mentioned in one chunk: ``(l)-[BELONGS_TO]->()-[MENTIONED_IN]->(chunk)``."""
    s = store._s
    rows = store._q(
        "read:chunk_statements",
        f"""SELECT DISTINCT bt.src AS l
            FROM {s}.edge mi
            JOIN {s}.edge bt ON bt.type = '__BELONGS_TO__' AND bt.dst = mi.src
            WHERE mi.type = '__MENTIONED_IN__' AND mi.dst = :cid
            ORDER BY bt.src LIMIT {_limit(params, "statementLimit", 1000)}""",
        {"cid": params["chunkId"]},
    )
    return [{"l": r["l"]} for r in rows]


def _is_chunk_top_entities(flat: str) -> bool:
    return flat.startswith("// get top entities for chunk (semantic beam search)")


def _h_chunk_top_entities(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Entities supporting statements in a chunk, ranked by their total subject/object edge count."""
    s = store._s
    rows = store._q(
        "read:chunk_top_entities",
        f"""WITH ents AS (
              SELECT DISTINCT sj.src AS eid
              FROM {s}.edge mi
              JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = mi.src
              JOIN {s}.edge sj ON sj.type IN {_SPO} AND sj.dst = su.src
              WHERE mi.type = '__MENTIONED_IN__' AND mi.dst = :cid)
            SELECT e.eid AS "entityId", count(r.dst) AS score
            FROM ents e JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = e.eid
            GROUP BY e.eid ORDER BY score DESC, e.eid LIMIT {_limit(params, "limit", 5)}""",
        {"cid": params["chunkId"]},
    )
    return [{"entityId": r["entityId"], "score": int(r["score"])} for r in rows]


def _is_chunk_neighbours(flat: str) -> bool:
    return flat.startswith("// get neighboring chunks for common entities (semantic beam search)")


def _h_chunk_neighbours(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Other chunks that mention the same entities, ranked by how many of those entities they share."""
    entity_ids = list(params.get("entityIds") or [])
    if not entity_ids:
        return []
    s = store._s
    rows = store._q(
        "read:chunk_neighbours",
        f"""SELECT x.cid AS "chunkId", count(*) AS shared
            FROM (SELECT DISTINCT mi.dst AS cid, sj.src AS eid
                  FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id)
                  JOIN {s}.edge sj ON sj.type IN {_SPO} AND sj.src = q.id
                  JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.src = sj.dst
                  JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.src = su.dst
                  WHERE mi.dst <> :cid) x
            GROUP BY x.cid ORDER BY shared DESC, x.cid LIMIT {_limit(params, "limit", 10)}""",
        {"ids": _ids_param(entity_ids), "cid": params["chunkId"]},
    )
    return [{"chunkId": r["chunkId"], "count(entity)": int(r["shared"])} for r in rows]


def _is_chunks_with_source(flat: str) -> bool:
    return flat.startswith(
        "MATCH (chunk:`__Chunk__`)-[:`__EXTRACTED_FROM__`]->(source:`__Source__`) WHERE chunk.chunkId in $chunk_ids"
    )


def _h_chunks_with_source(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Chunk and source property maps for chunk ids (``get_chunks_query``)."""
    ids = list(params.get("chunk_ids") or [])
    if not ids:
        return []
    s = store._s
    rows = _q_adaptive(
        store,
        "read:chunks_with_source",
        f"""SELECT c.id AS cid, c.value AS cvalue, c.search_str AS csearch, c.class AS cclass, c.props::text AS cprops,
                   so.id AS sid, so.props::text AS sprops
            FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS q(id)
            JOIN {s}.node c ON c.id = q.id AND c.label = '__Chunk__'
            JOIN {s}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = c.id
            JOIN {s}.node so ON so.id = ef.dst AND so.label = '__Source__'
            ORDER BY c.id""",
        ids,
        size=50,
    )
    out: list[Any] = []
    for r in rows:
        chunk = _props_map(
            {
                "id": r["cid"],
                "value": r["cvalue"],
                "search_str": r["csearch"],
                "class": r["cclass"],
                "props": r["cprops"],
            },
            "__Chunk__",
        )
        source = _props_map({"id": r["sid"], "props": r["sprops"]}, "__Source__", with_columns=False)
        out.append({"result": {"source": {"sourceId": r["sid"], "metadata": source}, "chunk": chunk}})
    return out


# ── remaining retrieval strategies, entity providers, utils, get_sources ─────────────────────────
# Each fixed-shape query is matched by exact text (after tenant-label canonicalisation and whitespace
# folding). A reworded or extended query therefore falls through to UnsupportedGraphQueryError.
_Q_CHUNK_VALUES = (
    "MATCH (chunk:`__Chunk__`) WHERE chunk.chunkId in $chunk_ids RETURN { chunk: chunk{.chunkId, .value} } AS result"
)
_Q_DEL_CHUNKS = "// delete chunks (delete source) MATCH (c) WHERE c.chunkId IN $chunkIds DELETE c"
_Q_DEL_CHUNK_RELS = (
    "// delete chunk relationships (delete source) MATCH (c)-[r]-() WHERE c.chunkId IN $chunkIds DELETE r"
)
_Q_DEL_ENTITIES = "// delete entities (delete source) MATCH (e) WHERE e.entityId IN $entityIds DELETE e"
_Q_DEL_ENTITY_RELS = (
    "// delete entity relationships (delete source) MATCH (e)-[r]-() WHERE e.entityId IN $entityIds DELETE r"
)
_Q_DEL_FACTS = "// delete facts (delete source) MATCH (f) WHERE f.factId IN $factIds DELETE f"
_Q_DEL_FACT_RELS = "// delete fact relationships (delete source) MATCH (f)-[r]-() WHERE f.factId IN $factIds DELETE r"
_Q_DEL_SOURCE = "// delete source (delete source) MATCH (s) WHERE s.sourceId = $sourceId DELETE s"
_Q_DEL_SOURCE_RELS = (
    "// delete source relationships (delete source) MATCH (s)-[r]-() WHERE s.sourceId = $sourceId DELETE r"
)
_Q_DEL_STATEMENTS = "// delete statements (delete source) MATCH (l) WHERE l.statementId IN $statementIds DELETE l"
_Q_DEL_STATEMENT_RELS = (
    "// delete statement relationships (delete source) MATCH (l)-[r]-() WHERE l.statementId IN $statementIds DELETE r"
)
_Q_DEL_TOPICS = "// delete topics (delete source) MATCH (t) WHERE t.topicId IN $topicIds DELETE t"
_Q_DEL_TOPIC_RELS = (
    "// delete topic relationships (delete source) MATCH (t)-[r]-() WHERE t.topicId IN $topicIds DELETE r"
)
_Q_ENTITIES_FOR_CHUNKS = "// get entities for chunk ids MATCH (c:`__Chunk__`)<-[:`__MENTIONED_IN__`]-(:`__Statement__`) <-[:`__SUPPORTS__`]-()<-[:`__SUBJECT__`|`__OBJECT__`]-(entity) WHERE c.chunkId in $nodeIds AND entity.class <> '__Local_Entity__' WITH DISTINCT entity MATCH (entity)-[r:`__SUBJECT__`|`__OBJECT__`]->() WITH entity, count(r) AS score ORDER BY score DESC LIMIT $limit RETURN { entity: entity{.entityId, .value, .class}, score: score } AS result"
_Q_ENTITIES_FOR_STATEMENT = "// Get entities for statement MATCH (s)<-[:`__SUPPORTS__`]-(f)<-[:`__SUBJECT__`|`__OBJECT__`]-(entity) WHERE s.statementId in $statementIds AND entity.class <> '__Local_Entity__' WITH DISTINCT entity OPTIONAL MATCH (entity)-[r:`__SUBJECT__`|`__OBJECT__`]->() WITH entity, count(r) AS score ORDER BY score DESC RETURN { entity: entity{.entityId, .value, .class}, score: score } AS result"
_Q_ENTITIES_FOR_TOPICS = "// get entities for topic ids MATCH (t:`__Topic__`)<-[:`__BELONGS_TO__`]-(:`__Statement__`) <-[:`__SUPPORTS__`]-()<-[:`__SUBJECT__`|`__OBJECT__`]-(entity) WHERE t.topicId in $nodeIds AND entity.class <> '__Local_Entity__' WITH DISTINCT entity MATCH (entity)-[r:`__SUBJECT__`|`__OBJECT__`]->() WITH entity, count(r) AS score ORDER BY score DESC LIMIT $limit RETURN { entity: entity{.entityId, .value, .class}, score: score } AS result"
_Q_ENTITY_MULTI = "// multiple entity-based graph search MATCH p=(e1:`__Entity__`{entityId:$startId})-[:`__RELATION__`*1..2]-(e2:`__Entity__`) WHERE e2.entityId in $endIds UNWIND nodes(p) AS n WITH DISTINCT COLLECT(n) AS entities MATCH (s)-[:`__SUBJECT__`]->(f)<-[:`__OBJECT__`]-(o), (f)-[:`__SUPPORTS__`]->()-[:`__PREVIOUS__`*0..1]-(l) WHERE s in entities and o in entities RETURN DISTINCT l.statementId AS l LIMIT $statementLimit"
_Q_ENTITY_SINGLE = "// single entity-based graph search MATCH (:`__Entity__`{entityId:$startId}) -[:`__SUBJECT__`]->() -[:`__SUPPORTS__`]->() -[:`__PREVIOUS__`*0..1]-(l) RETURN DISTINCT l.statementId AS l LIMIT $statementLimit"
_Q_EXPAND_ENTITIES = "// expand entities: score entities by number of relations MATCH (entity:`__Entity__`)-[r:`__SUBJECT__`|`__OBJECT__`]->() WHERE entity.entityId IN $entityIds WITH entity, count(r) AS score RETURN { entity: entity{.entityId, .value, .class}, score: score } AS result"
_Q_GET_CHUNK_IDS = "// get chunk ids (delete source) MATCH (s)<-[:`__EXTRACTED_FROM__`]-(c) WHERE s.sourceId = $sourceId RETURN DISTINCT c.chunkId AS chunkId LIMIT $batchSize"
_Q_GET_ENTITY_IDS = "// get entity ids (delete source) MATCH (f)<-[:`__SUBJECT__`|`__OBJECT__`]-(e) WHERE f.factId IN $factIds RETURN DISTINCT e.entityId AS entityId"
_Q_GET_FACT_IDS = "// get fact ids (delete source) MATCH (l)<-[:`__SUPPORTS__`]-(f) WHERE l.statementId IN $statementIds RETURN DISTINCT f.factId AS factId"
_Q_GET_ORPHAN_ENTITIES = "// get orphaned entity ids (delete source) MATCH (e) WHERE e.entityId IN $entityIds AND NOT (e)-[:`__SUBJECT__`|`__OBJECT__`]->() RETURN DISTINCT e.entityId AS entityId"
_Q_GET_ORPHAN_FACTS = "// get orphaned fact ids (delete source) MATCH (f) WHERE f.factId IN $factIds AND NOT (f)-[:`__SUPPORTS__`]->() RETURN DISTINCT f.factId AS factId"
_Q_GET_STATEMENT_IDS = "// get statement ids (delete source) MATCH (s)<-[:`__EXTRACTED_FROM__`]-()<-[:`__MENTIONED_IN__`]-(l:`__Statement__`) WHERE s.sourceId = $sourceId RETURN DISTINCT l.statementId AS statementId LIMIT $batchSize"
_Q_GET_TOPIC_IDS = "// get topic ids (delete source) MATCH (s)<-[:`__EXTRACTED_FROM__`]-()<-[:`__MENTIONED_IN__`]-(t:`__Topic__`) WHERE s.sourceId = $sourceId RETURN DISTINCT t.topicId AS topicId LIMIT $batchSize"
_Q_NETWORK_CHUNK = "// chunk-based entity network search MATCH (l)-[:`__BELONGS_TO__`]->()-[:`__MENTIONED_IN__`]->(c:`__Chunk__`) WHERE c.chunkId = $nodeId RETURN DISTINCT l.statementId AS l LIMIT $statementLimit"
_Q_NETWORK_TOPIC = "// topic-based entity network search MATCH (l)-[:`__BELONGS_TO__`]->(t:`__Topic__`) WHERE t.topicId = $nodeId RETURN DISTINCT l.statementId AS l LIMIT $statementLimit"
_Q_STATEMENTS_WITH_SOURCE = "MATCH (statement:`__Statement__`)-[:`__MENTIONED_IN__`]->(chunk:`__Chunk__`)-[:`__EXTRACTED_FROM__`]->(source:`__Source__`) WHERE statement.statementId in $statement_ids RETURN { statement: statement{.*}, source: { sourceId: source.sourceId, metadata: source{.*} }, chunk: chunk{.*} } AS result"
_Q_TOPIC_BASED = "// topic-based graph search MATCH (f)-[:`__SUPPORTS__`]->()-[:`__BELONGS_TO__`]->(tt:`__Topic__`) WHERE tt.topicId = $topicId WITH f LIMIT $statementLimit MATCH (f)-[:`__SUPPORTS__`]->(l) RETURN DISTINCT l.statementId AS l LIMIT $statementLimit"
_Q_TOPIC_CONTENT = "// get topic content MATCH (t:`__Topic__`)<-[:`__BELONGS_TO__`]-(s)<-[r:`__SUPPORTS__`]-() WHERE t.topicId = $topicId WITH s, count(r) AS score ORDER BY score DESC RETURN s.value AS statement, s.details AS details LIMIT $statementLimit"
_Q_TOPIC_SOURCES = "MATCH (t:`__Topic__`)-[:`__MENTIONED_IN__`]->(c:`__Chunk__`)-[:`__EXTRACTED_FROM__`]->(s:`__Source__`) WHERE t.topicId IN $topicIds RETURN DISTINCT t.topicId AS topicId, s.sourceId AS sourceId"
_Q_TOP_STATEMENTS_CHUNK = "// Get statements for top chunk MATCH (c:`__Chunk__`)<-[:`__MENTIONED_IN__`]-(s:`__Statement__`) WHERE c.chunkId in $nodeIds RETURN { statement: s.value, statementId: id(s) } AS result"
_Q_TOP_STATEMENTS_TOPIC = "// Get statements for top chunk MATCH (t:`__Topic__`)<-[:`__MENTIONED_IN__`]-(s:`__Statement__`) WHERE t.topicId in $nodeIds RETURN { statement: s.value, statementId: id(s) } AS result"
_Q_TREE_LEVEL = "// get next level in tree MATCH (entity:`__Entity__`)-[:`__RELATION__`]->(other) -[r:`__SUBJECT__`|`__OBJECT__`]->() WHERE entity.entityId IN $entityIds AND NOT other.entityId IN $excludeEntityIds AND other.class <> '__Local_Entity__' WITH entity, other, count(r) AS score ORDER BY score DESC WITH entity, collect(DISTINCT other.entityId)[0..$numNeighbours] AS others RETURN { entity: entity{.entityId, .value, .class}, others: others } AS result"

_LOCAL = "'__Local_Entity__'"
_EDGE_TYPES = (
    "__BELONGS_TO__",
    "__CHILD__",
    "__EXTRACTED_FROM__",
    "__MENTIONED_IN__",
    "__NEXT__",
    "__OBJECT__",
    "__PARENT__",
    "__PREVIOUS__",
    "__RELATION__",
    "__SUBJECT__",
    "__SUPPORTS__",
    "__SYS_RELATION__",
)
_EDGE_TYPES_SQL = ", ".join(f"'{t}'" for t in _EDGE_TYPES)


def _exact(text: str) -> Any:
    return lambda flat: flat == text


def _ids_cte(name: str = "q") -> str:
    return f"jsonb_array_elements_text(CAST(:ids AS jsonb)) AS {name}(id)"


def _previous_expansion(src_cte: str) -> str:
    """Statement ids in ``src_cte(sid)`` plus their ``PREVIOUS`` neighbours in either direction (``*0..1``)."""
    return f"""SELECT sid AS l FROM {src_cte}
              UNION SELECT p.dst FROM {src_cte} JOIN {{s}}.edge p ON p.type = '__PREVIOUS__' AND p.src = {src_cte}.sid
              UNION SELECT p.src FROM {src_cte} JOIN {{s}}.edge p ON p.type = '__PREVIOUS__' AND p.dst = {src_cte}.sid"""


def _ids_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [{key: r[key]} for r in rows]


# entity_based ----------------------------------------------------------------------------------
def _h_entity_multi(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Statements joining entities on any ``RELATION*1..2`` path between ``startId`` and ``endIds``."""
    end_ids = list(params.get("endIds") or [])
    if not end_ids:
        return []
    s = store._s
    prev = _previous_expansion("st").format(s=s)
    sql = f"""WITH nb AS (
              SELECT e.dst AS n FROM {s}.edge e WHERE e.type = '__RELATION__' AND e.src = :start
              UNION SELECT e.src FROM {s}.edge e WHERE e.type = '__RELATION__' AND e.dst = :start),
            ends AS (SELECT q.id FROM {_ids_cte()} JOIN {s}.node en ON en.id = q.id AND en.label = '__Entity__'),
            p1 AS (SELECT nb.n AS e2 FROM nb JOIN ends ON ends.id = nb.n),
            nb2 AS (
              SELECT nb.n AS mid, x.n FROM nb CROSS JOIN LATERAL (
                SELECT e.dst AS n FROM {s}.edge e WHERE e.type = '__RELATION__' AND e.src = nb.n
                UNION SELECT e.src FROM {s}.edge e WHERE e.type = '__RELATION__' AND e.dst = nb.n) x),
            p2 AS (SELECT nb2.mid, nb2.n AS e2 FROM nb2 JOIN ends ON ends.id = nb2.n),
            ent AS (
              SELECT CAST(:start AS text) AS id
                WHERE EXISTS (SELECT 1 FROM {s}.node st WHERE st.id = :start AND st.label = '__Entity__')
                  AND (EXISTS (SELECT 1 FROM p1) OR EXISTS (SELECT 1 FROM p2))
              UNION SELECT e2 FROM p1
              UNION SELECT e2 FROM p2
              UNION SELECT mid FROM p2),
            st AS (
              SELECT DISTINCT su.dst AS sid
              FROM ent a
              JOIN {s}.edge sj ON sj.type = '__SUBJECT__' AND sj.src = a.id
              JOIN {s}.edge oj ON oj.type = '__OBJECT__' AND oj.dst = sj.dst
              JOIN ent b ON b.id = oj.src
              JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.src = sj.dst),
            x AS ({prev})
            SELECT l FROM x ORDER BY l LIMIT {_limit(params, "statementLimit", 10)}"""
    rows = store._q("read:entity_multi", sql, {"start": params["startId"], "ids": _ids_param(end_ids)})
    return _ids_rows(rows, "l")


def _h_entity_single(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Statements supported by facts the entity is the subject of, plus ``PREVIOUS`` neighbours."""
    s = store._s
    prev = _previous_expansion("st").format(s=s)
    sql = f"""WITH st AS (
              SELECT DISTINCT su.dst AS sid
              FROM {s}.node en
              JOIN {s}.edge sj ON sj.type = '__SUBJECT__' AND sj.src = en.id
              JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.src = sj.dst
              WHERE en.id = :start AND en.label = '__Entity__'),
            x AS ({prev})
            SELECT l FROM x ORDER BY l LIMIT {_limit(params, "statementLimit", 10)}"""
    return _ids_rows(store._q("read:entity_single", sql, {"start": params["startId"]}), "l")


# entity_network, topic_based -------------------------------------------------------------------
def _h_network_topic(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    rows = store._q(
        "read:network_topic",
        f"""SELECT DISTINCT bt.src AS l FROM {store._s}.edge bt
            WHERE bt.type = '__BELONGS_TO__' AND bt.dst = :nid
            ORDER BY bt.src LIMIT {_limit(params, "statementLimit", 10)}""",
        {"nid": params["nodeId"]},
    )
    return _ids_rows(rows, "l")


def _h_network_chunk(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    rows = store._q(
        "read:network_chunk",
        f"""SELECT DISTINCT bt.src AS l
            FROM {store._s}.edge mi
            JOIN {store._s}.edge bt ON bt.type = '__BELONGS_TO__' AND bt.dst = mi.src
            WHERE mi.type = '__MENTIONED_IN__' AND mi.dst = :cid
            ORDER BY bt.src LIMIT {_limit(params, "statementLimit", 10)}""",
        {"cid": params["nodeId"]},
    )
    return _ids_rows(rows, "l")


def _h_topic_based(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """The first ``statementLimit`` (fact, statement) rows of a topic, then every statement each fact supports."""
    s = store._s
    n = _limit(params, "statementLimit", 10)
    rows = store._q(
        "read:topic_based",
        f"""WITH f AS (
              SELECT su.src AS fid
              FROM {s}.edge bt JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = bt.src
              WHERE bt.type = '__BELONGS_TO__' AND bt.dst = :tid
              ORDER BY su.src, bt.src LIMIT {n})
            SELECT DISTINCT su2.dst AS l
            FROM f JOIN {s}.edge su2 ON su2.type = '__SUPPORTS__' AND su2.src = f.fid
            ORDER BY l LIMIT {n}""",
        {"tid": params["topicId"]},
    )
    return _ids_rows(rows, "l")


# entity providers ------------------------------------------------------------------------------
_ENT_KEYWORDS = re.compile(
    r"^// get entities for keywords MATCH \(entity:`__Entity__`\)-\[r:`__SUBJECT__`\|`__OBJECT__`\]->\(\) "
    r"WHERE entity\.search_str (?P<op>=|STARTS WITH) \$keyword "
    r"(?:and entity\.class (?P<cop>=|STARTS WITH) \$classification|AND entity\.class <> '__Local_Entity__') "
    r"WITH entity, count\(r\) AS score ORDER BY score DESC "
    r"RETURN \{ entity: entity\{\.entityId, \.value, \.class\}, score: score \} AS result$"
)


def _entity_results(rows: list[dict[str, Any]]) -> list[Any]:
    return [
        {
            "result": {
                "entity": {"entityId": r["entityId"], "value": r["value"], "class": r["cls"]},
                "score": int(r["score"]),
            }
        }
        for r in rows
    ]


def _like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _is_entities_for_keywords(flat: str) -> bool:
    return bool(_ENT_KEYWORDS.match(flat))


def _h_entities_for_keywords(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = _ENT_KEYWORDS.match(flat)
    if not m:
        raise UnsupportedGraphQueryError(f"entity keyword shape changed: {flat[:300]}")
    bind: dict[str, Any] = {"tenant": tenant}
    where = ["n.label = '__Entity__'", "n.tenant = :tenant"]
    if m.group("op") == "=":
        where.append("n.search_str = :kw")
        bind["kw"] = params["keyword"]
    else:
        where.append("n.search_str LIKE :kw")
        bind["kw"] = _like_prefix(params["keyword"])
    if m.group("cop") is None:
        where.append(f"n.class <> {_LOCAL}")
    elif m.group("cop") == "=":
        where.append("n.class = :cls")
        bind["cls"] = params["classification"]
    else:
        where.append("n.class LIKE :cls")
        bind["cls"] = _like_prefix(params["classification"])
    s = store._s
    rows = store._q(
        "read:entities_for_keywords",
        f"""SELECT n.id AS "entityId", n.value AS value, n.class AS cls, count(*) AS score
            FROM {s}.node n JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = n.id
            WHERE {" AND ".join(where)}
            GROUP BY n.id, n.value, n.class ORDER BY score DESC, n.id""",
        bind,
    )
    return _entity_results(rows)


def _h_entities_for_nodes(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Entities behind the statements of topics (or chunks), scored by their subject/object edge count."""
    ids = list(params.get("nodeIds") or [])
    if not ids:
        return []
    s = store._s
    if flat == _Q_ENTITIES_FOR_TOPICS:
        hop = f"""JOIN {s}.node t ON t.id = q.id AND t.label = '__Topic__'
              JOIN {s}.edge e1 ON e1.type = '__BELONGS_TO__' AND e1.dst = t.id
              JOIN {s}.node st ON st.id = e1.src AND st.label = '__Statement__'"""
    else:
        hop = f"""JOIN {s}.node c ON c.id = q.id AND c.label = '__Chunk__'
              JOIN {s}.edge e1 ON e1.type = '__MENTIONED_IN__' AND e1.dst = c.id
              JOIN {s}.node st ON st.id = e1.src AND st.label = '__Statement__'"""
    rows = store._q(
        "read:entities_for_nodes",
        f"""WITH ents AS (
              SELECT DISTINCT sj.src AS eid
              FROM {_ids_cte()} {hop}
              JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = st.id
              JOIN {s}.edge sj ON sj.type IN {_SPO} AND sj.dst = su.src
              JOIN {s}.node en ON en.id = sj.src AND en.class <> {_LOCAL})
            SELECT en.id AS "entityId", en.value AS value, en.class AS cls, count(*) AS score
            FROM ents JOIN {s}.node en ON en.id = ents.eid
            JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = en.id
            GROUP BY en.id, en.value, en.class ORDER BY score DESC, en.id LIMIT {_limit(params, "limit", 10)}""",
        {"ids": _ids_param(ids)},
    )
    return _entity_results(rows)


def _h_entities_for_statement(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("statementIds") or [])
    if not ids:
        return []
    s = store._s
    rows = store._q(
        "read:entities_for_statement",
        f"""WITH ents AS (
              SELECT DISTINCT sj.src AS eid
              FROM {_ids_cte()}
              JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = q.id
              JOIN {s}.edge sj ON sj.type IN {_SPO} AND sj.dst = su.src
              JOIN {s}.node en ON en.id = sj.src AND en.class <> {_LOCAL})
            SELECT en.id AS "entityId", en.value AS value, en.class AS cls, count(r.dst) AS score
            FROM ents JOIN {s}.node en ON en.id = ents.eid
            LEFT JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = en.id
            GROUP BY en.id, en.value, en.class ORDER BY score DESC, en.id""",
        {"ids": _ids_param(ids)},
    )
    return _entity_results(rows)


def _h_expand_entities(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("entityIds") or [])
    if not ids:
        return []
    s = store._s
    rows = store._q(
        "read:expand_entities",
        f"""SELECT en.id AS "entityId", en.value AS value, en.class AS cls, count(*) AS score
            FROM {_ids_cte()}
            JOIN {s}.node en ON en.id = q.id AND en.label = '__Entity__'
            JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = en.id
            GROUP BY en.id, en.value, en.class ORDER BY en.id""",
        {"ids": _ids_param(ids)},
    )
    return _entity_results(rows)


def _h_tree_level(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Per entity, the ``numNeighbours`` most connected ``RELATION`` neighbours outside the excluded set."""
    ids = list(params.get("entityIds") or [])
    if not ids:
        return []
    s = store._s
    n = max(0, int(params.get("numNeighbours") or 0))
    rows = store._q(
        "read:tree_level",
        f"""WITH pairs AS (
              SELECT rel.src AS eid, rel.dst AS oid, count(*) AS score
              FROM {_ids_cte()}
              JOIN {s}.edge rel ON rel.type = '__RELATION__' AND rel.src = q.id
              JOIN {s}.node o ON o.id = rel.dst AND o.class <> {_LOCAL}
              JOIN {s}.edge r ON r.type IN {_SPO} AND r.src = rel.dst
              WHERE rel.dst NOT IN (SELECT x.id FROM jsonb_array_elements_text(CAST(:excl AS jsonb)) AS x(id))
              GROUP BY rel.src, rel.dst),
            ranked AS (
              SELECT eid, oid, row_number() OVER (PARTITION BY eid ORDER BY score DESC, oid) AS rn FROM pairs)
            SELECT e.id AS "entityId", e.value AS value, e.class AS cls,
                   jsonb_agg(ranked.oid ORDER BY ranked.rn)::text AS others
            FROM ranked JOIN {s}.node e ON e.id = ranked.eid AND e.label = '__Entity__'
            WHERE ranked.rn <= {n}
            GROUP BY e.id, e.value, e.class ORDER BY e.id""",
        {"ids": _ids_param(ids), "excl": _ids_param(params.get("excludeEntityIds") or [])},
    )
    return [
        {
            "result": {
                "entity": {"entityId": r["entityId"], "value": r["value"], "class": r["cls"]},
                "others": json.loads(r["others"]),
            }
        }
        for r in rows
    ]


def _h_top_statements(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    """Statements ``MENTIONED_IN`` the given topics (or chunks), as ``{statement, statementId}`` maps."""
    ids = list(params.get("nodeIds") or [])
    if not ids:
        return []
    s = store._s
    label = "__Topic__" if flat == _Q_TOP_STATEMENTS_TOPIC else "__Chunk__"
    rows = store._q(
        "read:top_statements",
        f"""SELECT st.id AS "statementId", st.value AS statement
            FROM {_ids_cte()}
            JOIN {s}.node t ON t.id = q.id AND t.label = '{label}'
            JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.dst = t.id
            JOIN {s}.node st ON st.id = mi.src AND st.label = '__Statement__'
            ORDER BY st.id""",
        {"ids": _ids_param(ids)},
    )
    return [{"result": {"statement": r["statement"], "statementId": r["statementId"]}} for r in rows]


def _h_topic_content(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    s = store._s
    return store._q(
        "read:topic_content",
        f"""SELECT st.value AS statement, COALESCE(st.props ->> 'details', '') AS details
            FROM {s}.edge bt
            JOIN {s}.node st ON st.id = bt.src
            JOIN {s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = bt.src
            WHERE bt.type = '__BELONGS_TO__' AND bt.dst = :tid
            GROUP BY st.id, st.value, st.props ->> 'details'
            ORDER BY count(*) DESC, st.id LIMIT {_limit(params, "statementLimit", 10)}""",
        {"tid": params["topicId"]},
    )


# utils (chunk_utils, statement_utils, vector_utils, in_graph_chunk_store) ------------------------
def _h_chunk_values(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("chunk_ids") or [])
    if not ids:
        return []
    rows = _q_adaptive(
        store,
        "read:chunk_values",
        f"""SELECT c.id AS "chunkId", c.value AS value
            FROM {_ids_cte()} JOIN {store._s}.node c ON c.id = q.id AND c.label = '__Chunk__'
            ORDER BY c.id""",
        ids,
        size=50,
    )
    return [{"result": {"chunk": {"chunkId": r["chunkId"], "value": r["value"]}}} for r in rows]


def _h_statements_with_source(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("statement_ids") or [])
    if not ids:
        return []
    s = store._s
    rows = _q_adaptive(
        store,
        "read:statements_with_source",
        f"""SELECT st.id AS stid, st.value AS stvalue, st.props::text AS stprops,
                   c.id AS cid, c.value AS cvalue, c.search_str AS csearch, c.class AS cclass, c.props::text AS cprops,
                   so.id AS sid, so.props::text AS sprops
            FROM {_ids_cte()}
            JOIN {s}.node st ON st.id = q.id AND st.label = '__Statement__'
            JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.src = st.id
            JOIN {s}.node c ON c.id = mi.dst AND c.label = '__Chunk__'
            JOIN {s}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = c.id
            JOIN {s}.node so ON so.id = ef.dst AND so.label = '__Source__'
            ORDER BY st.id, c.id, so.id""",
        ids,
        size=50,
    )
    out: list[Any] = []
    for r in rows:
        statement = _props_map({"id": r["stid"], "value": r["stvalue"], "props": r["stprops"]}, "__Statement__")
        chunk = _props_map(
            {
                "id": r["cid"],
                "value": r["cvalue"],
                "search_str": r["csearch"],
                "class": r["cclass"],
                "props": r["cprops"],
            },
            "__Chunk__",
        )
        source = _props_map({"id": r["sid"], "props": r["sprops"]}, "__Source__", with_columns=False)
        out.append(
            {
                "result": {
                    "statement": statement,
                    "source": {"sourceId": r["sid"], "metadata": source},
                    "chunk": chunk,
                }
            }
        )
    return out


def _h_topic_sources(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("topicIds") or [])
    if not ids:
        return []
    s = store._s
    return _q_adaptive(
        store,
        "read:topic_sources",
        f"""SELECT DISTINCT t.id AS "topicId", so.id AS "sourceId"
            FROM {_ids_cte()}
            JOIN {s}.node t ON t.id = q.id AND t.label = '__Topic__'
            JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.src = t.id
            JOIN {s}.node c ON c.id = mi.dst AND c.label = '__Chunk__'
            JOIN {s}.edge ef ON ef.type = '__EXTRACTED_FROM__' AND ef.src = c.id
            JOIN {s}.node so ON so.id = ef.dst AND so.label = '__Source__'
            ORDER BY t.id, so.id""",
        ids,
    )


# get_sources (LexicalGraphIndex.get_sources; used by graph cleanup and DeletePrevVersions) -----
_GET_SOURCES = re.compile(
    r"^// get source info from source ids MATCH \(source:`__Source__`\) (?:WHERE (?P<where>.+?) )?"
    r"RETURN \{ sourceId: source\.sourceId, metadata: properties\(source\), versioning: \{ "
    r"valid_from: coalesce\(source\.(?P<vf>\w+), (?P<vfd>-?\d+)\), valid_to: coalesce\(source\.(?P<vt>\w+), (?P<vtd>-?\d+)\), "
    r"extract_timestamp: coalesce\(source\.(?P<et>\w+), (?P<etd>-?\d+)\), build_timestamp: coalesce\(source\.(?P<bt>\w+), (?P<btd>-?\d+)\), "
    r'id_fields: split\(coalesce\(source\.(?P<idf>\w+), ""\), ";"\), prev_versions: split\(coalesce\(source\.(?P<pv>\w+), ""\), ";"\) '
    r"\} \} AS result ORDER BY result\.versioning\.valid_from ASC$"
)
_SOURCE_IDS_CLAUSE = "(source.sourceId in $sourceIds)"


def _is_get_sources(flat: str) -> bool:
    return flat.startswith("// get source info from source ids")


def _h_get_sources(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    m = _GET_SOURCES.match(flat)
    if not m:  # includes ``order_by`` (extra sort keys), which COA never passes
        raise UnsupportedGraphQueryError(f"get_sources shape changed: {flat[:300]}")
    where = m.group("where") or ""
    conditions = ["n.label = '__Source__'", "n.tenant = :tenant"]
    bind: dict[str, Any] = {"tenant": tenant, "vf": m.group("vf")}
    rest = where
    if rest.startswith(_SOURCE_IDS_CLAUSE):
        ids = list(params.get("sourceIds") or [])
        if not ids:
            return []
        conditions.append("n.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))")
        bind["ids"] = _ids_param(ids)
        rest = rest[len(_SOURCE_IDS_CLAUSE) :]
        if rest and not rest.startswith(" AND "):
            raise UnsupportedGraphQueryError(f"get_sources filter shape changed: {where[:300]}")
        rest = rest[len(" AND ") :]
    if rest:
        sql_filter, fparams = translate_source_filter(rest)
        conditions.append(sql_filter)
        bind.update(fparams)
    rows = store._q(
        "read:get_sources",
        f"""SELECT n.id AS "sourceId", n.props::text AS props
            FROM {store._s}.node n WHERE {" AND ".join(conditions)}
            ORDER BY COALESCE((n.props ->> :vf)::bigint, {int(m.group("vfd"))}) ASC, n.id""",
        bind,
    )
    out = []
    for r in rows:
        meta = _props_map({"id": r["sourceId"], "props": r["props"]}, "__Source__", with_columns=False)
        out.append(
            {
                "result": {
                    "sourceId": r["sourceId"],
                    "metadata": meta,
                    "versioning": {
                        "valid_from": meta.get(m.group("vf"), int(m.group("vfd"))),
                        "valid_to": meta.get(m.group("vt"), int(m.group("vtd"))),
                        "extract_timestamp": meta.get(m.group("et"), int(m.group("etd"))),
                        "build_timestamp": meta.get(m.group("bt"), int(m.group("btd"))),
                        "id_fields": str(meta.get(m.group("idf")) or "").split(";"),
                        "prev_versions": str(meta.get(m.group("pv")) or "").split(";"),
                    },
                }
            }
        )
    return out


# DeleteSources -----------------------------------------------------------------------------------
def _delete_get_chunk_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    return _delete_source_children(store, params, label=None, key="chunkId")


def _delete_get_topic_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    return _delete_source_children(store, params, label="__Topic__", key="topicId")


def _delete_get_statement_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    return _delete_source_children(store, params, label="__Statement__", key="statementId")


def _delete_source_children(store: AuroraGraphStore, params: dict[str, Any], label: str | None, key: str) -> list[Any]:
    """Chunks of a source (``label`` None), or the topics/statements mentioned in those chunks."""
    s = store._s
    limit = _limit(params, "batchSize", 1000)
    if label is None:
        sql = f"""SELECT DISTINCT ef.src AS "{key}" FROM {s}.edge ef
                  WHERE ef.type = '__EXTRACTED_FROM__' AND ef.dst = :sid ORDER BY 1 LIMIT {limit}"""
    else:
        sql = f"""SELECT DISTINCT mi.src AS "{key}"
                  FROM {s}.edge ef
                  JOIN {s}.edge mi ON mi.type = '__MENTIONED_IN__' AND mi.dst = ef.src
                  JOIN {s}.node x ON x.id = mi.src AND x.label = '{label}'
                  WHERE ef.type = '__EXTRACTED_FROM__' AND ef.dst = :sid ORDER BY 1 LIMIT {limit}"""
    return _ids_rows(store._q("read:delete_ids", sql, {"sid": params["sourceId"]}), key)


def _delete_get_fact_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("statementIds") or [])
    if not ids:
        return []
    rows = _q_adaptive(
        store,
        "read:delete_ids",
        f"""SELECT DISTINCT su.src AS "factId" FROM {_ids_cte()}
            JOIN {store._s}.edge su ON su.type = '__SUPPORTS__' AND su.dst = q.id ORDER BY 1""",
        ids,
    )
    return _ids_rows(rows, "factId")


def _delete_get_entity_ids(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    ids = list(params.get("factIds") or [])
    if not ids:
        return []
    rows = _q_adaptive(
        store,
        "read:delete_ids",
        f"""SELECT DISTINCT sj.src AS "entityId" FROM {_ids_cte()}
            JOIN {store._s}.edge sj ON sj.type IN {_SPO} AND sj.dst = q.id ORDER BY 1""",
        ids,
    )
    return _ids_rows(rows, "entityId")


def _delete_orphans(store: AuroraGraphStore, ids: list[str], label: str, edge_types: str, key: str) -> list[Any]:
    """Ids among ``ids`` of ``label`` nodes with no outgoing edge of ``edge_types`` (``NOT (n)-[..]->()``)."""
    if not ids:
        return []
    s = store._s
    rows = _q_adaptive(
        store,
        "read:delete_ids",
        f"""SELECT n.id AS "{key}" FROM {_ids_cte()}
            JOIN {s}.node n ON n.id = q.id AND n.label = '{label}'
            WHERE NOT EXISTS (SELECT 1 FROM {s}.edge e WHERE e.type IN {edge_types} AND e.src = n.id)
            ORDER BY 1""",
        ids,
    )
    return _ids_rows(rows, key)


def _delete_get_orphan_facts(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    return _delete_orphans(store, list(params.get("factIds") or []), "__Fact__", "('__SUPPORTS__')", "factId")


def _delete_get_orphan_entities(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
    return _delete_orphans(store, list(params.get("entityIds") or []), "__Entity__", _SPO, "entityId")


def _delete_edges(store: AuroraGraphStore, ids: list[str]) -> None:
    """``MATCH (n)-[r]-() WHERE n.<id> IN $ids DELETE r``. The type filter lets Postgres use the edge indexes."""
    if not ids:
        return
    s = store._s
    store._q(
        "write:delete_edges",
        f"""DELETE FROM {s}.edge e USING {_ids_cte()}
            WHERE e.type IN ({_EDGE_TYPES_SQL}) AND (e.src = q.id OR e.dst = q.id)""",
        {"ids": _ids_param(ids)},
    )


def _delete_nodes(store: AuroraGraphStore, ids: list[str], label: str) -> None:
    if not ids:
        return
    store._q(
        "write:delete_nodes",
        f"DELETE FROM {store._s}.node n USING {_ids_cte()} WHERE n.id = q.id AND n.label = '{label}'",
        {"ids": _ids_param(ids)},
    )


def _make_delete(param: str, label: str, edges: bool) -> Any:
    def handler(store: AuroraGraphStore, flat: str, tenant: str, params: dict[str, Any]) -> list[Any]:
        ids = [params[param]] if param == "sourceId" else list(params.get(param) or [])
        if edges:
            _delete_edges(store, ids)
        else:
            _delete_nodes(store, ids, label)
        return []

    return handler


_READ_HANDLERS: list[tuple[str, Any, Any]] = [
    ("statements_by_topic_and_source", _is_statements_by_topic, _h_statements_by_topic),
    ("facts_for_statements", lambda f: f.startswith("// get facts for statements"), _h_facts),
    ("topic_statements", _is_expand_topics, _h_expand_topics),
    ("topic_neighbors", _is_topic_neighbors, _h_topic_neighbors),
    ("source_id_width_read", _is_width_read, _h_width_read),
    ("source_id_width_write", _is_width_write, _h_width_write),
    ("source_sample", _is_source_sample, _h_source_sample),
    ("version_find", _is_version_find, _h_version_find),
    ("version_ids", _is_version_ids, _h_version_ids),
    ("version_set", _is_version_set, _h_version_set),
    ("local_subject_complements", _is_local_subject_complements, _h_local_subject_complements),
    ("local_complement_subjects", _is_local_complement_subjects, _h_local_complement_subjects),
    ("local_copy", _is_local_copy, _h_local_copy),
    ("local_delete", _is_local_delete, _h_local_delete),
    ("source_collision", _is_source_collision, _h_source_collision),
    ("chunk_statements", _is_chunk_statements, _h_chunk_statements),
    ("chunk_top_entities", _is_chunk_top_entities, _h_chunk_top_entities),
    ("chunk_neighbours", _is_chunk_neighbours, _h_chunk_neighbours),
    ("chunks_with_source", _is_chunks_with_source, _h_chunks_with_source),
    ("entity_multi", _exact(_Q_ENTITY_MULTI), _h_entity_multi),
    ("entity_single", _exact(_Q_ENTITY_SINGLE), _h_entity_single),
    ("network_topic", _exact(_Q_NETWORK_TOPIC), _h_network_topic),
    ("network_chunk", _exact(_Q_NETWORK_CHUNK), _h_network_chunk),
    ("topic_based", _exact(_Q_TOPIC_BASED), _h_topic_based),
    ("entities_for_keywords", _is_entities_for_keywords, _h_entities_for_keywords),
    ("entities_for_topics", _exact(_Q_ENTITIES_FOR_TOPICS), _h_entities_for_nodes),
    ("entities_for_chunks", _exact(_Q_ENTITIES_FOR_CHUNKS), _h_entities_for_nodes),
    ("top_statements_topic", _exact(_Q_TOP_STATEMENTS_TOPIC), _h_top_statements),
    ("top_statements_chunk", _exact(_Q_TOP_STATEMENTS_CHUNK), _h_top_statements),
    ("entities_for_statement", _exact(_Q_ENTITIES_FOR_STATEMENT), _h_entities_for_statement),
    ("expand_entities", _exact(_Q_EXPAND_ENTITIES), _h_expand_entities),
    ("tree_level", _exact(_Q_TREE_LEVEL), _h_tree_level),
    ("topic_content", _exact(_Q_TOPIC_CONTENT), _h_topic_content),
    ("chunk_values", _exact(_Q_CHUNK_VALUES), _h_chunk_values),
    ("statements_with_source", _exact(_Q_STATEMENTS_WITH_SOURCE), _h_statements_with_source),
    ("topic_sources", _exact(_Q_TOPIC_SOURCES), _h_topic_sources),
    ("get_sources", _is_get_sources, _h_get_sources),
    ("delete_get_chunk_ids", _exact(_Q_GET_CHUNK_IDS), _delete_get_chunk_ids),
    ("delete_get_topic_ids", _exact(_Q_GET_TOPIC_IDS), _delete_get_topic_ids),
    ("delete_get_statement_ids", _exact(_Q_GET_STATEMENT_IDS), _delete_get_statement_ids),
    ("delete_get_fact_ids", _exact(_Q_GET_FACT_IDS), _delete_get_fact_ids),
    ("delete_get_entity_ids", _exact(_Q_GET_ENTITY_IDS), _delete_get_entity_ids),
    ("delete_get_orphan_facts", _exact(_Q_GET_ORPHAN_FACTS), _delete_get_orphan_facts),
    ("delete_get_orphan_entities", _exact(_Q_GET_ORPHAN_ENTITIES), _delete_get_orphan_entities),
    ("delete_entity_edges", _exact(_Q_DEL_ENTITY_RELS), _make_delete("entityIds", "__Entity__", True)),
    ("delete_entities", _exact(_Q_DEL_ENTITIES), _make_delete("entityIds", "__Entity__", False)),
    ("delete_fact_edges", _exact(_Q_DEL_FACT_RELS), _make_delete("factIds", "__Fact__", True)),
    ("delete_facts", _exact(_Q_DEL_FACTS), _make_delete("factIds", "__Fact__", False)),
    ("delete_statement_edges", _exact(_Q_DEL_STATEMENT_RELS), _make_delete("statementIds", "__Statement__", True)),
    ("delete_statements", _exact(_Q_DEL_STATEMENTS), _make_delete("statementIds", "__Statement__", False)),
    ("delete_topic_edges", _exact(_Q_DEL_TOPIC_RELS), _make_delete("topicIds", "__Topic__", True)),
    ("delete_topics", _exact(_Q_DEL_TOPICS), _make_delete("topicIds", "__Topic__", False)),
    ("delete_chunk_edges", _exact(_Q_DEL_CHUNK_RELS), _make_delete("chunkIds", "__Chunk__", True)),
    ("delete_chunks", _exact(_Q_DEL_CHUNKS), _make_delete("chunkIds", "__Chunk__", False)),
    ("delete_source_edges", _exact(_Q_DEL_SOURCE_RELS), _make_delete("sourceId", "__Source__", True)),
    ("delete_source", _exact(_Q_DEL_SOURCE), _make_delete("sourceId", "__Source__", False)),
]


_WRITE_SHAPE = re.compile(r"UNWIND\s+\$params\s+AS\s+params")


def classify(cypher: str) -> str | None:
    """Name the handler a query reaches (``"write"`` for the write interpreter), or None when unsupported."""
    canon, _tenant = canonicalize(cypher)
    flat = " ".join(canon.split())
    for name, pred, _handler in _READ_HANDLERS:
        if pred(flat):
            return name
    if _WRITE_SHAPE.search(canon) and _MERGE_NODE.search(canon):
        return "write"
    return None


# ── factory ──────────────────────────────────────────────────────────────────
class AuroraGraphStoreFactory(GraphStoreFactoryMethod):
    """Claims ``aurora-pg://[?schema=lg]``."""

    def try_create(self, graph_info: str, **kwargs: Any) -> GraphStore | None:
        """Return a store for an ``aurora-pg://`` URI, else None."""
        if not graph_info.startswith(SCHEME):
            return None
        q = parse_qs(urlparse(graph_info).query)
        schema = (q.get("schema") or ["lg"])[0]
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid schema name in {SCHEME} URI")
        return AuroraGraphStore(schema_name=schema, log_formatting=get_log_formatting(kwargs))


def register() -> None:
    """Register the factory with the toolkit (idempotent)."""
    from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory

    GraphStoreFactory.register(AuroraGraphStoreFactory)
