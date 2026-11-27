# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""SPARQL over the Aurora ``coa_triple`` quad table (``GRAPH_BACKEND=aurora``).

Neptune answers SPARQL natively. On the Aurora path the same queries run through rdflib: the
graphs a query names are loaded from ``coa_triple`` into an in-memory ``rdflib.Dataset`` and the
query is evaluated there. Graph scope comes from the query text itself, which is how every query
the product generates already scopes itself:

* ``GRAPH <iri>``, ``WITH <iri>``, ``FROM [NAMED] <iri>``, ``USING [NAMED] <iri>``: that exact graph.
* ``FILTER(STRSTARTS(STR(?g), "prefix"))``: every graph whose IRI starts with ``prefix``
  (``graph_uri LIKE 'prefix%'``).
* Neither: the whole table (bounded by ``max_load_triples``).

Loaded graphs are cached per scope for ``ttl_s`` seconds and bounded by a total triple count.
``update()`` takes a transaction with advisory locks on the scope, loads it fresh, runs the update
with rdflib, diffs the quads and writes the difference with the row semantics of
``coa_ontology.stores.aurora.graph`` (unique key ``(graph_uri, s, p, o_kind, md5(o), o_lang,
o_datatype)``, ``xsd:string`` stored as ``''``). The cache is dropped after every update.

Result shapes follow the serve ``NeptuneGraphClient``: flat ``{var: value}`` dicts for SELECT,
a bool for ASK, ``{subject, predicate, object}`` dicts for CONSTRUCT. ``query_json`` returns SPARQL
JSON results for callers (metric-service) that read ``results.bindings`` directly.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import rdflib
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.term import Node

from coa_common.aurora_data_api import (
    AuroraDataApiClient,
    Transaction,
    build_client_from_env,
    bulk_json_chunks,
    json_text,
)

log = logging.getLogger(__name__)

GRAPH_BACKEND_ENV = "GRAPH_BACKEND"
GRAPH_BACKEND_AURORA = "aurora"
DEFAULT_MAX_RESULTS = 10_000

_XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
_PAGE_SIZE = 500

_IRI_SCOPE = re.compile(
    r"\b(?:GRAPH|WITH|FROM(?:\s+NAMED)?|USING(?:\s+NAMED)?)\s*<([^<>\s]+)>",
    re.IGNORECASE,
)
_PREFIX_SCOPE = re.compile(
    r'STRSTARTS\s*\(\s*STR\s*\(\s*\?\w+\s*\)\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)',
    re.IGNORECASE,
)
_ESCAPE = re.compile(r"\\(.)")

_LOAD_COLUMNS = "id, namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype"

Scope = tuple[tuple[str, ...], tuple[str, ...]]  # (exact graph IRIs, graph IRI prefixes), both sorted

# A quad as stored: (graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype)
_Quad = tuple[str, str, str, str, str, str, str, str]


class AuroraSparqlError(RuntimeError):
    """A SPARQL query or update could not be served from Aurora."""


def graph_backend() -> str:
    """The configured graph backend (``GRAPH_BACKEND``), lower-cased; empty means Neptune."""
    return os.environ.get(GRAPH_BACKEND_ENV, "").strip().lower()


def aurora_graph_enabled() -> bool:
    """True when ``GRAPH_BACKEND=aurora``."""
    return graph_backend() == GRAPH_BACKEND_AURORA


def graph_base() -> str:
    """Base IRI under which per-namespace graph IRIs live (same rule as the ontology engine)."""
    base = os.environ.get("NDB_GRAPH_URI_BASE", "").strip().rstrip("/")
    if base:
        return base
    legacy = os.environ.get("NDB_GRAPH_URI", "").strip()
    if legacy:
        if "/graph/" in legacy:
            legacy = legacy.rsplit("/graph/", 1)[0]
        return legacy.rstrip("/")
    return "https://ontology-workbench.local"


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: m.group(1), text)


def scope_of(sparql: str) -> Scope:
    """The graphs a query or update names: exact IRIs and ``STRSTARTS`` prefixes."""
    exact = sorted({m.group(1) for m in _IRI_SCOPE.finditer(sparql)})
    prefixes = sorted({_unescape(m.group(1)) for m in _PREFIX_SCOPE.finditer(sparql)})
    # A prefix covers every exact graph beneath it.
    exact = [g for g in exact if not any(g.startswith(p) for p in prefixes)]
    return tuple(exact), tuple(prefixes)


def _like_escape(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _node(kind: str, value: str, lang: str = "", datatype: str = "") -> Node:
    if kind == "bnode":
        return rdflib.BNode(value)
    if kind == "literal":
        if lang:
            return rdflib.Literal(value, lang=lang)
        return rdflib.Literal(value, datatype=rdflib.URIRef(datatype) if datatype else None)
    return rdflib.URIRef(value)


def _kind(node: Node) -> str:
    if isinstance(node, rdflib.BNode):
        return "bnode"
    return "literal" if isinstance(node, rdflib.Literal) else "iri"


def _quad_of(graph_uri: str, s: Node, p: Node, o: Node) -> _Quad:
    lang = datatype = ""
    if isinstance(o, rdflib.Literal):
        lang = o.language or ""
        datatype = str(o.datatype) if o.datatype else ""
        if datatype == _XSD_STRING:
            datatype = ""
    return (graph_uri, str(s), _kind(s), str(p), str(o), _kind(o), lang, datatype)


def _quad_row(r: Mapping[str, Any]) -> _Quad:
    datatype = r["o_datatype"] or ""
    if datatype == _XSD_STRING:
        datatype = ""
    return (r["graph_uri"], r["s"], r["s_kind"], r["p"], r["o"], r["o_kind"], r["o_lang"] or "", datatype)


class _Loaded:
    """One loaded scope: the dataset, its stored quads and the namespace of each graph."""

    def __init__(self) -> None:
        self.dataset = rdflib.Dataset(default_union=True)
        self.quads: set[_Quad] = set()
        self.namespaces: dict[str, str] = {}
        self.loaded_at = time.monotonic()

    @property
    def size(self) -> int:
        return len(self.quads)

    def add(self, row: Mapping[str, Any]) -> None:
        quad = _quad_row(row)
        if quad in self.quads:
            return
        self.quads.add(quad)
        g, s, s_kind, p, o, o_kind, lang, datatype = quad
        self.namespaces.setdefault(g, row.get("namespace") or "")
        self.dataset.add(
            (_node(s_kind, s), rdflib.URIRef(p), _node(o_kind, o, lang, datatype), rdflib.URIRef(g)),
        )


def _json_value(term: Node) -> dict[str, str]:
    if isinstance(term, rdflib.BNode):
        return {"type": "bnode", "value": str(term)}
    if isinstance(term, rdflib.Literal):
        out = {"type": "literal", "value": str(term)}
        if term.language:
            out["xml:lang"] = term.language
        elif term.datatype:
            out["datatype"] = str(term.datatype)
        return out
    return {"type": "uri", "value": str(term)}


class AuroraSparql:
    """Evaluate SPARQL against ``coa_triple`` through the Data API (blocking; thread safe)."""

    def __init__(
        self,
        client: AuroraDataApiClient | None = None,
        *,
        ttl_s: float | None = None,
        max_cached_triples: int | None = None,
        max_load_triples: int | None = None,
    ) -> None:
        """Bind a Data API client (built from the environment on first use when omitted)."""
        self._client = client
        self._ttl_s = ttl_s if ttl_s is not None else float(os.environ.get("AURORA_SPARQL_CACHE_TTL_S", "30"))
        self._max_cached = (
            max_cached_triples
            if max_cached_triples is not None
            else int(os.environ.get("AURORA_SPARQL_CACHE_MAX_TRIPLES", "500000"))
        )
        self._max_load = (
            max_load_triples
            if max_load_triples is not None
            else int(os.environ.get("AURORA_SPARQL_MAX_LOAD_TRIPLES", "2000000"))
        )
        self._cache: OrderedDict[Scope, _Loaded] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._load_locks: dict[Scope, threading.Lock] = {}
        self._generation = 0

    # ── plumbing ────────────────────────────────────────────────────
    def _db(self) -> AuroraDataApiClient:
        if self._client is None:
            self._client = build_client_from_env()
        return self._client

    def invalidate(self) -> None:
        """Drop every cached graph."""
        with self._cache_lock:
            self._cache.clear()
            self._generation += 1

    @staticmethod
    def _where(scope: Scope) -> tuple[str, dict[str, Any]]:
        exact, prefixes = scope
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if exact:
            clauses.append("graph_uri IN (SELECT jsonb_array_elements_text(CAST(:graphs AS jsonb)))")
            params["graphs"] = json_text(list(exact))
        for i, prefix in enumerate(prefixes):
            clauses.append(f"graph_uri LIKE :pfx{i} ESCAPE '\\'")
            params[f"pfx{i}"] = _like_escape(prefix)
        if not clauses:
            return "TRUE", {}
        return "(" + " OR ".join(clauses) + ")", params

    def _load(self, scope: Scope, tx: Transaction | None = None) -> _Loaded:
        where, params = self._where(scope)
        sql = f"SELECT {_LOAD_COLUMNS} FROM coa_triple WHERE {where} AND id > :after ORDER BY id LIMIT :limit"

        def build(last: Mapping[str, Any] | None, limit: int) -> tuple[str, dict[str, Any]]:
            return sql, {**params, "after": last["id"] if last else 0, "limit": limit}

        loaded = _Loaded()
        for page in self._db().iter_pages(build, page_size=_PAGE_SIZE, tx=tx):
            for row in page:
                loaded.add(row)
            if loaded.size > self._max_load:
                raise AuroraSparqlError(
                    f"the graph scope holds more than {self._max_load} triples; narrow the query's GRAPH scope"
                )
        return loaded

    def _loaded(self, scope: Scope) -> _Loaded:
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(scope)
            if hit is not None and now - hit.loaded_at <= self._ttl_s:
                self._cache.move_to_end(scope)
                return hit
            lock = self._load_locks.setdefault(scope, threading.Lock())
        with lock:
            with self._cache_lock:
                hit = self._cache.get(scope)
                if hit is not None and time.monotonic() - hit.loaded_at <= self._ttl_s:
                    return hit
                generation = self._generation
            loaded = self._load(scope)
            with self._cache_lock:
                if generation == self._generation and loaded.size <= self._max_cached:
                    self._cache[scope] = loaded
                    self._cache.move_to_end(scope)
                    total = sum(e.size for e in self._cache.values())
                    while total > self._max_cached and len(self._cache) > 1:
                        _, evicted = self._cache.popitem(last=False)
                        total -= evicted.size
            return loaded

    # ── reads ───────────────────────────────────────────────────────
    def _run(self, sparql: str) -> rdflib.query.Result:
        loaded = self._loaded(scope_of(sparql))
        try:
            return loaded.dataset.query(sparql)
        except Exception as exc:  # rdflib raises parser/evaluator errors of many types
            raise AuroraSparqlError(f"SPARQL query failed: {type(exc).__name__}: {exc}") from exc

    def query_json(self, sparql: str) -> dict[str, Any]:
        """SELECT or ASK as SPARQL JSON results (``results.bindings`` / ``boolean``)."""
        result = self._run(sparql)
        if result.type == "ASK":
            return {"head": {}, "boolean": bool(result.askAnswer)}
        variables = [str(v) for v in result.vars or []]
        bindings = []
        for row in result.bindings:
            bindings.append({str(k): _json_value(v) for k, v in row.items() if v is not None})
        return {"head": {"vars": variables}, "results": {"bindings": bindings}}

    def query(self, sparql: str, *, max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, Any]]:
        """SELECT: flat ``{var: value}`` dicts, with a LIMIT appended when the query has none."""
        if "LIMIT" not in sparql.upper():
            sparql = f"{sparql.rstrip().rstrip(';')} LIMIT {int(max_results)}"
        data = self.query_json(sparql)
        return [{k: v.get("value", "") for k, v in row.items()} for row in data.get("results", {}).get("bindings", [])]

    def ask(self, sparql: str) -> bool:
        """ASK."""
        return bool(self.query_json(sparql).get("boolean", False))

    def construct(self, sparql: str, *, max_triples: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]]:
        """CONSTRUCT / DESCRIBE: ``{subject, predicate, object}`` dicts in N-Triples order, capped."""
        result = self._run(sparql)
        triples = sorted(set(result.graph), key=lambda t: tuple(term.n3() for term in t))
        if len(triples) > max_triples:
            log.warning("aurora_construct_truncated triples=%d max=%d", len(triples), max_triples)
            triples = triples[:max_triples]
        return [{"subject": str(s), "predicate": str(p), "object": str(o)} for s, p, o in triples]

    def health_check(self) -> dict[str, Any]:
        """Probe the Data API; never raises."""
        try:
            self._db().execute("SELECT 1 AS ok")
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 - health checks report, never raise
            return {"status": "error", "detail": str(exc)}

    # ── writes ──────────────────────────────────────────────────────
    @staticmethod
    def _namespace_for(graph_uri: str, known: Mapping[str, str]) -> str:
        if known.get(graph_uri):
            return known[graph_uri]
        base = graph_base() + "/"
        if graph_uri.startswith(base):
            return graph_uri[len(base) :].split("/", 1)[0]
        segments = [seg for seg in urlparse(graph_uri).path.split("/") if seg]
        if len(segments) >= 2:
            return segments[-2]
        raise AuroraSparqlError(f"cannot derive a namespace for graph {graph_uri!r}; it is not under {base}")

    def update(self, sparql: str) -> dict[str, int]:
        """Run a SPARQL UPDATE and write the quad difference in one transaction.

        Returns ``{"inserted": n, "deleted": n}``.
        """
        scope = scope_of(sparql)
        db = self._db()
        try:
            with db.transaction() as tx:
                for key in [f"coa-sparql:g:{g}" for g in scope[0]] + [f"coa-sparql:p:{p}" for p in scope[1]] or [
                    "coa-sparql:all"
                ]:
                    tx.execute("SELECT pg_advisory_xact_lock(hashtext(:k)) AS locked", {"k": key})
                loaded = self._load(scope, tx)
                try:
                    loaded.dataset.update(sparql)
                except Exception as exc:
                    raise AuroraSparqlError(f"SPARQL update failed: {type(exc).__name__}: {exc}") from exc
                after: set[_Quad] = set()
                for s, p, o, ctx in loaded.dataset.quads((None, None, None, None)):
                    graph_uri = str(getattr(ctx, "identifier", ctx))
                    # Default-graph data has no graph_uri to store under; only named graphs persist.
                    if graph_uri == str(DATASET_DEFAULT_GRAPH_ID):
                        continue
                    after.add(_quad_of(graph_uri, s, p, o))
                inserted = after - loaded.quads
                deleted = loaded.quads - after
                if deleted:
                    self._delete_quads(tx, deleted)
                if inserted:
                    self._insert_quads(tx, inserted, loaded.namespaces)
        finally:
            self.invalidate()
        return {"inserted": len(inserted), "deleted": len(deleted)}

    @staticmethod
    def _delete_quads(tx: Transaction, quads: set[_Quad]) -> None:
        rows = [
            {"g": g, "s": s, "p": p, "o": o, "o_kind": ok, "o_lang": lang, "o_datatype": dt}
            for g, s, _sk, p, o, ok, lang, dt in sorted(quads)
        ]
        sql = (
            "DELETE FROM coa_triple t USING jsonb_to_recordset(CAST(:rows AS jsonb)) AS r("
            "g text, s text, p text, o text, o_kind text, o_lang text, o_datatype text) "
            "WHERE t.graph_uri = r.g AND t.s = r.s AND t.p = r.p AND t.o_kind = r.o_kind "
            "AND md5(t.o) = md5(r.o) AND t.o_lang = r.o_lang AND t.o_datatype = r.o_datatype"
        )
        for chunk in bulk_json_chunks(rows):
            tx.execute(sql, {"rows": chunk})

    def _insert_quads(self, tx: Transaction, quads: set[_Quad], known: Mapping[str, str]) -> None:
        rows = [
            {
                "ns": self._namespace_for(g, known),
                "g": g,
                "s": s,
                "s_kind": sk,
                "p": p,
                "o": o,
                "o_kind": ok,
                "o_lang": lang,
                "o_datatype": dt,
            }
            for g, s, sk, p, o, ok, lang, dt in sorted(quads)
        ]
        sql = (
            "INSERT INTO coa_triple (namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype) "
            "SELECT r.ns, r.g, r.s, r.s_kind, r.p, r.o, r.o_kind, r.o_lang, r.o_datatype "
            "FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r("
            "ns text, g text, s text, s_kind text, p text, o text, o_kind text, o_lang text, o_datatype text) "
            "ON CONFLICT (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype) DO NOTHING"
        )
        for chunk in bulk_json_chunks(rows):
            tx.execute(sql, {"rows": chunk})

    def turtle(self, graph_uri: str) -> str | None:
        """One graph as Turtle; ``None`` when it holds no triples."""
        loaded = self._load(((graph_uri,), ()))
        if not loaded.size:
            return None
        graph = rdflib.Graph()
        for _g, s, sk, p, o, ok, lang, dt in loaded.quads:
            graph.add((_node(sk, s), rdflib.URIRef(p), _node(ok, o, lang, dt)))
        return graph.serialize(format="turtle")


_shared: AuroraSparql | None = None
_shared_lock = threading.Lock()


def shared_sparql() -> AuroraSparql:
    """Process-wide :class:`AuroraSparql` (one cache per process)."""
    global _shared  # noqa: PLW0603
    with _shared_lock:
        if _shared is None:
            _shared = AuroraSparql()
        return _shared


__all__ = [
    "GRAPH_BACKEND_AURORA",
    "GRAPH_BACKEND_ENV",
    "AuroraSparql",
    "AuroraSparqlError",
    "aurora_graph_enabled",
    "graph_backend",
    "graph_base",
    "scope_of",
    "shared_sparql",
]
