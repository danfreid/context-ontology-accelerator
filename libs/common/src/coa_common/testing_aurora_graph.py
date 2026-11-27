# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-memory stand-in for the Data API client over ``coa_triple`` (tests only).

:class:`InMemoryTripleClient` answers the statements :class:`coa_common.aurora_sparql.AuroraSparql`
issues (scoped keyset loads, advisory locks, bulk delete and insert through ``jsonb_to_recordset``)
from a Python list. It runs the real ``AuroraSparql`` code, rdflib evaluation and quad diff, but no
SQL: unique-index conflicts are modelled by the quad tuple, and paging is a single page. Real
Postgres semantics (``LIKE``, ``md5`` index, keyset paging) are covered by
``libs/common/tests/integ/test_aurora_sparql_pg.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import rdflib

from coa_common.aurora_sparql import AuroraSparql

_Row = dict[str, Any]


def _like_prefix(pattern: str) -> str:
    return pattern.rstrip("%").replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\")


class InMemoryTripleClient:
    """Data API client double holding ``coa_triple`` rows."""

    def __init__(self) -> None:
        """Start with an empty table."""
        self.rows: list[_Row] = []
        self._next_id = 1
        self.statements: list[tuple[str, Mapping[str, Any]]] = []
        self.transactions = 0

    # ── seeding / inspection ────────────────────────────────────────
    def add_triple(
        self,
        graph_uri: str,
        s: str,
        p: str,
        o: str,
        *,
        o_kind: str = "iri",
        namespace: str = "ns",
        s_kind: str = "iri",
        o_lang: str = "",
        o_datatype: str = "",
    ) -> None:
        """Insert one quad (ignored when it already exists)."""
        row = {
            "namespace": namespace,
            "graph_uri": graph_uri,
            "s": s,
            "s_kind": s_kind,
            "p": p,
            "o": o,
            "o_kind": o_kind,
            "o_lang": o_lang,
            "o_datatype": o_datatype,
        }
        if any(self._key(r) == self._key(row) for r in self.rows):
            return
        self.rows.append({"id": self._next_id, **row})
        self._next_id += 1

    def load_turtle(self, turtle: str, graph_uri: str, namespace: str = "ns") -> None:
        """Parse Turtle and store its triples in ``graph_uri``."""
        parsed = rdflib.Graph()
        parsed.parse(data=turtle, format="turtle")
        for s, p, o in parsed:
            kind = "bnode" if isinstance(o, rdflib.BNode) else "literal" if isinstance(o, rdflib.Literal) else "iri"
            lang = o.language or "" if isinstance(o, rdflib.Literal) else ""
            datatype = str(o.datatype) if isinstance(o, rdflib.Literal) and o.datatype else ""
            self.add_triple(
                graph_uri,
                str(s),
                str(p),
                str(o),
                o_kind=kind,
                namespace=namespace,
                s_kind="bnode" if isinstance(s, rdflib.BNode) else "iri",
                o_lang=lang,
                o_datatype=datatype,
            )

    def graph_triples(self, graph_uri: str) -> set[tuple[str, str, str, str]]:
        """``(s, p, o, o_kind)`` of every stored triple in one graph."""
        return {(r["s"], r["p"], r["o"], r["o_kind"]) for r in self.rows if r["graph_uri"] == graph_uri}

    @staticmethod
    def _key(r: Mapping[str, Any]) -> tuple[str, ...]:
        return (r["graph_uri"], r["s"], r["p"], r["o_kind"], r["o"], r["o_lang"], r["o_datatype"])

    # ── Data API surface used by AuroraSparql ───────────────────────
    def _select(self, params: Mapping[str, Any]) -> list[_Row]:
        graphs = set(json.loads(params["graphs"])) if "graphs" in params else set()
        prefixes = [_like_prefix(v) for k, v in params.items() if k.startswith("pfx")]
        scoped = "graphs" in params or bool(prefixes)
        out = [
            r
            for r in self.rows
            if r["id"] > params.get("after", 0)
            and (not scoped or r["graph_uri"] in graphs or any(r["graph_uri"].startswith(p) for p in prefixes))
        ]
        return sorted(out, key=lambda r: r["id"])

    def iter_pages(
        self, build: Callable[[Mapping[str, Any] | None, int], tuple[str, Mapping[str, Any]]], **_: Any
    ) -> Iterator[list[_Row]]:
        """One page holding every matching row."""
        sql, params = build(None, 10**9)
        self.statements.append((sql, params))
        rows = self._select(params)
        if rows:
            yield [dict(r) for r in rows]

    def execute(self, sql: str, params: Mapping[str, Any] | None = None, *, tx: Any = None) -> list[_Row]:
        """Answer ``SELECT 1`` and the statements of a transaction."""
        self.statements.append((sql, params or {}))
        text = sql.lstrip()
        if text.startswith("DELETE FROM coa_triple"):
            keys = {self._key({**r, "graph_uri": r["g"]}) for r in json.loads(params["rows"])}  # type: ignore[index]
            self.rows = [r for r in self.rows if self._key(r) not in keys]
        elif text.startswith("INSERT INTO coa_triple"):
            for r in json.loads(params["rows"]):  # type: ignore[index]
                self.add_triple(
                    r["g"],
                    r["s"],
                    r["p"],
                    r["o"],
                    o_kind=r["o_kind"],
                    namespace=r["ns"],
                    s_kind=r["s_kind"],
                    o_lang=r["o_lang"],
                    o_datatype=r["o_datatype"],
                )
        return [{"ok": 1}] if text.startswith("SELECT 1 AS ok") else []

    @contextmanager
    def transaction(self) -> Iterator[_Tx]:
        """A transaction whose writes apply immediately and roll back when the block raises."""
        self.transactions += 1
        snapshot = list(self.rows)
        try:
            yield _Tx(self)
        except BaseException:
            self.rows = snapshot
            raise


class _Tx:
    def __init__(self, client: InMemoryTripleClient) -> None:
        self._client = client

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[_Row]:
        return self._client.execute(sql, params)


def make_sparql(client: InMemoryTripleClient | None = None, **kwargs: Any) -> tuple[AuroraSparql, InMemoryTripleClient]:
    """An :class:`AuroraSparql` over an in-memory table (a fresh one unless ``client`` is given)."""
    client = client or InMemoryTripleClient()
    return AuroraSparql(client, **kwargs), client  # type: ignore[arg-type]
