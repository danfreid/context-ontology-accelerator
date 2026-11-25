# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Graph-traversal golden questions: every answer is recomputed from the committed corpus files.

The graph is rebuilt here from ``corpus/universe.json`` (ownership edges, accounts), the scenario manifests
(messages, attachments, deals) and the structured CSVs (invoices, payments, agreement limits), with a
different algorithm from ``traversal.py`` (path enumeration instead of breadth-first search).
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from typing import Any

import pytest

from tests.benchmark.fsi_commodity.build import CORPUS_DIR
from tests.benchmark.fsi_commodity.golden import GOLDEN_PATH

pytestmark = pytest.mark.unit

Edges = list[tuple[str, str, str]]


def _csv(name: str) -> list[dict[str, str]]:
    with (CORPUS_DIR / "structured" / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class CorpusGraph:
    """Typed edges and labels read from the committed corpus files."""

    def __init__(self) -> None:
        universe = json.loads((CORPUS_DIR / "universe.json").read_text(encoding="utf-8"))
        self.universe = universe
        self.edges: Edges = []
        self.types: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        for oid, o in universe["orgs"].items():
            self._node(oid, o["kind"], o["name"])
        for pid, p in universe["persons"].items():
            if p["kind"] == "owner":
                self._node(pid, "person", p["name"])
        self.edges += [(e["parent"], e["child"], "owns") for e in universe["ownership_edges"]]
        for aid, a in universe["accounts"].items():
            self._node(aid, "account", aid)
            self.edges += [(a["owner"], aid, "has_account"), (aid, a["bank"], "held_at")]
        self._manifests()
        self._structured()

    def _node(self, node: str, type_: str, label: str | None = None) -> None:
        self.types[node] = type_
        self.labels[node] = label or node

    def _manifests(self) -> None:
        for path in sorted(CORPUS_DIR.glob("*/*/manifest.json")):
            m = json.loads(path.read_text(encoding="utf-8"))
            ids = {msg["key"]: msg["message_id"] for msg in m["messages"]}
            for d in m["deals"]:
                self._node(d["trade_id"], "trade")
                self.edges += [(d["entity"], d["trade_id"], "party_to"), (d["counterparty"], d["trade_id"], "party_to")]
            for a in m["attachments"]:
                trade = a["facts"].get("trade_id")
                if trade is None:
                    continue
                att = f"{m['scenario']}/{a['filename']}"
                self._node(ids[a["message"]], "email")
                self._node(att, "attachment", a["filename"])
                self.edges += [(ids[a["message"]], att, "attaches"), (att, trade, "about")]

    def _structured(self) -> None:
        payment_of = {p["trade_id"]: p["payment_id"] for p in _csv("payments")}
        for inv in _csv("invoices"):
            self._node(inv["document_number"], "invoice")
            self.edges.append((inv["trade_id"], inv["document_number"], "billed"))
            if inv["trade_id"] in payment_of:
                self._node(payment_of[inv["trade_id"]], "payment")
                self.edges.append((inv["document_number"], payment_of[inv["trade_id"]], "settled_by"))
        names = {o["name"]: oid for oid, o in self.universe["orgs"].items()}
        for row in _csv("limits"):
            title = row["agreement"]
            if not title:
                continue
            signers = sorted(oid for name, oid in names.items() if name in title)
            if row["entity_org_id"] in signers:
                continue
            agr = f"agr:{title}"
            self._node(agr, "agreement", title)
            self.edges += [(s, agr, "signed") for s in signers]
            self.edges += [(row["entity_org_id"], agr, "acceded"), (agr, row["counterparty_org_id"], "sub_limit")]

    def paths(self, start: str, kinds: list[str], *, direction: str = "out") -> dict[str, list[list[str]]]:
        """Every simple path from ``start`` along ``kinds`` (reversed when ``direction`` is ``in``), by end node."""
        step: dict[str, list[str]] = {}
        for src, dst, kind in self.edges:
            if kind in kinds:
                a, b = (src, dst) if direction == "out" else (dst, src)
                step.setdefault(a, []).append(b)
        found: dict[str, list[list[str]]] = {}

        def walk(trail: list[str]) -> None:
            for nxt in step.get(trail[-1], ()):
                if nxt not in trail:
                    found.setdefault(nxt, []).append([*trail, nxt])
                    walk([*trail, nxt])

        walk([start])
        return found

    def shortest(self, start: str, end: str, kinds: list[str]) -> list[list[str]]:
        options = self.paths(start, kinds).get(end, [])
        best = min((len(p) for p in options), default=0)
        return [p for p in options if len(p) == best]


@pytest.fixture(scope="module")
def graph() -> CorpusGraph:
    return CorpusGraph()


@pytest.fixture(scope="module")
def traversal() -> dict[str, dict[str, Any]]:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in golden.items() if k.startswith("graph-q")}


def _expected(g: CorpusGraph, entry: dict[str, Any]) -> tuple[Any, int | None]:
    """Recompute ``(answer, hops)`` for one entry's ``query`` on the test graph."""
    q = entry["query"]
    if q["op"] == "reach":
        by_end = g.paths(q["start"], q["kinds"], direction=q["direction"])
        hit = {n: min(len(p) for p in ps) - 1 for n, ps in by_end.items() if g.types[n] in q["want"]}
        return sorted(g.labels[n] for n in hit), max(hit.values())
    if q["op"] == "share":
        total = Decimal(0)
        for path in g.paths(q["start"], ["owns"]).get(q["end"], []):
            share = Decimal(100)
            for parent, child in zip(path, path[1:], strict=False):
                pct = next(
                    e["percent"] for e in g.universe["ownership_edges"] if (e["parent"], e["child"]) == (parent, child)
                )
                share = share * Decimal(str(pct)) / 100
            total += share
        return float(total), len(g.shortest(q["start"], q["end"], ["owns"])[0]) - 1
    best = g.shortest(q["start"], q["end"], q["kinds"])
    if q["op"] == "exists":
        return ("yes", len(best[0]) - 1) if best else ("no", None)
    assert len(best) == 1, "path and hop questions need a unique shortest path"
    hops = len(best[0]) - 1
    return ([g.labels[n] for n in best[0]] if q["op"] == "path" else hops), hops


def test_traversal_question_count_and_ids(traversal: dict[str, dict[str, Any]]) -> None:
    assert len(traversal) >= 40
    assert list(traversal) == [f"graph-q{i:02d}" for i in range(1, len(traversal) + 1)]
    assert len({e["question"] for e in traversal.values()}) == len(traversal)


def test_traversal_range_of_depths_and_shapes(traversal: dict[str, dict[str, Any]]) -> None:
    hops = [e["hops"] for e in traversal.values() if e["hops"] is not None]
    assert min(hops) <= 2
    assert sum(1 for h in hops if h >= 4) >= 10
    assert max(hops) >= 6
    assert {e["shape"] for e in traversal.values()} >= {"reachable", "path", "path_length", "path_exists"}
    assert all(e["type"] == "graph_traversal" for e in traversal.values())


def test_traversal_entries_follow_the_compare_py_format(traversal: dict[str, dict[str, Any]]) -> None:
    from tests.benchmark.compare import is_correct

    for qid, e in traversal.items():
        answer = e["answer"]
        assert e["expected"] == (len(answer) if isinstance(answer, list) else 1), qid
        assert e["tolerance"] == 0
        assert e["tolerance_pct"] == 0
        results = [{"v": x} for x in (answer if isinstance(answer, list) else [answer])]
        assert is_correct({"status": 200, "body": {"results": results}}, e), qid
        assert not is_correct({"status": 200, "body": {"results": []}}, e), qid


def test_every_traversal_answer_matches_a_recomputation_from_the_corpus_files(
    graph: CorpusGraph, traversal: dict[str, dict[str, Any]]
) -> None:
    for qid, entry in traversal.items():
        answer, hops = _expected(graph, entry)
        assert entry["answer"] == answer, qid
        assert entry["hops"] == hops, qid


def test_known_ownership_facts_from_the_brief(traversal: dict[str, dict[str, Any]], graph: CorpusGraph) -> None:
    shares = {
        e["query"]["start"] + ">" + e["query"]["end"]: e["answer"]
        for e in traversal.values()
        if e["shape"] == "effective_share"
    }
    assert shares["own-founder-1>newco-hou"] == 34.1
    assert shares["own-founder-2>newco-syd"] == 27.9
    assert shares["own-tiber-hidden>cp-tiberpeak"] == 60.0
    tiber = next(e for e in traversal.values() if e["query"].get("end") == "cp-tiberpeak" and e["shape"] == "path")
    assert len(tiber["answer"]) == 5  # sanctioned owner, foundation, nominee, holding, Tiber Peak: 4 hops
    no_path = next(e for e in traversal.values() if e["shape"] == "path_exists" and e["answer"] == "no")
    assert graph.shortest(no_path["query"]["start"], no_path["query"]["end"], no_path["query"]["kinds"]) == []
