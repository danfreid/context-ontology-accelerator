# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Multi-hop graph-traversal questions, computed from the built universe and scenario data.

``traversal_questions`` builds one typed, directed graph from facts the corpus already holds and answers each
question by walking it, so no expected answer is typed by hand. Edges (``kind``: source -> target):

* ``owns``: owner -> owned entity (``universe.ownership_edges``)
* ``has_account``: organisation -> bank account; ``held_at``: account -> bank (``universe.accounts``)
* ``party_to``: NewCo entity or counterparty -> trade (the trade phase's deals)
* ``attaches``: email -> attachment; ``about``: attachment -> trade (messages, attachments and their ``trade_id``)
* ``billed``: trade -> its cash-bearing post-trade document; ``settled_by``: document -> payment
* ``signed``: organisation -> master agreement; ``acceded``: organisation -> master agreement it accedes to;
  ``sub_limit``: master agreement -> counterparty that has a sub-limit under it (scenario s5)

Every entry carries a machine-readable ``query`` (operation, start, edge kinds, wanted node types) so a test
can replay it on a graph it builds itself from the committed corpus files.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections import deque
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

from .structured import table_rows
from .universe import beneficial_owners

if TYPE_CHECKING:
    from .build import BuildResult

OWNERSHIP = ("owns",)
BANKING = ("owns", "has_account", "held_at")
LINEAGE = ("attaches", "about", "billed", "settled_by")
EXPOSURE = ("owns", "party_to", "billed", "settled_by")
ACCESSION = ("signed", "acceded", "sub_limit")
LEGAL_ENTITY_TYPES = ("newco", "counterparty", "holder")


class Graph:
    """A directed multigraph with typed nodes, display labels and edge kinds."""

    def __init__(self) -> None:
        self.types: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.out: dict[str, list[tuple[str, str]]] = {}
        self.inc: dict[str, list[tuple[str, str]]] = {}

    def node(self, node_id: str, type_: str, label: str | None = None) -> None:
        self.types[node_id] = type_
        self.labels[node_id] = label or node_id

    def edge(self, src: str, dst: str, kind: str) -> None:
        self.out.setdefault(src, []).append((dst, kind))
        self.inc.setdefault(dst, []).append((src, kind))

    def _next(self, node: str, kinds: Collection[str], direction: str) -> list[str]:
        table = self.out if direction == "out" else self.inc
        return sorted({n for n, k in table.get(node, ()) if k in kinds})

    def distances(self, start: str, kinds: Collection[str], direction: str = "out") -> dict[str, int]:
        """Shortest hop count from ``start`` to every node it reaches (``start`` itself excluded)."""
        dist = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for nxt in self._next(node, kinds, direction):
                if nxt not in dist:
                    dist[nxt] = dist[node] + 1
                    queue.append(nxt)
        del dist[start]
        return dist

    def shortest_paths(self, start: str, end: str, kinds: Collection[str]) -> list[list[str]]:
        """Every shortest directed path from ``start`` to ``end`` (empty when unreachable)."""
        dist = {start: 0}
        parents: dict[str, list[str]] = {start: []}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for nxt in self._next(node, kinds, "out"):
                if nxt not in dist:
                    dist[nxt] = dist[node] + 1
                    parents[nxt] = [node]
                    queue.append(nxt)
                elif dist[nxt] == dist[node] + 1:
                    parents[nxt].append(node)
        if end not in dist:
            return []

        def unwind(node: str) -> list[list[str]]:
            if node == start:
                return [[start]]
            return [[*p, node] for parent in parents[node] for p in unwind(parent)]

        return unwind(end)


def build_graph(result: BuildResult) -> Graph:
    """Build the graph from the universe and the scenarios' own data."""
    g = Graph()
    u = result.universe
    rows = table_rows(result)
    for oid, o in u["orgs"].items():
        g.node(oid, o["kind"], o["name"])
    for pid, p in u["persons"].items():
        if p["kind"] == "owner":
            g.node(pid, "person", p["name"])
    for e in u["ownership_edges"]:
        g.edge(e["parent"], e["child"], "owns")
    for aid, a in u["accounts"].items():
        g.node(aid, "account")
        g.edge(a["owner"], aid, "has_account")
        g.edge(aid, a["bank"], "held_at")
    payment_of = {r["trade_id"]: r["payment_id"] for r in rows["payments"]}
    for sc in result.scenarios:
        if sc.phase == "trade":
            for d in sc.data["deals"]:
                g.node(d["trade_id"], "trade")
                g.edge(d["entity"], d["trade_id"], "party_to")
                g.edge(d["counterparty"], d["trade_id"], "party_to")
        if sc.phase in ("trade", "post_trade", "settlement"):
            messages = {m["key"]: m for m in sc.messages}
            for a in sc.attachments:
                trade_id = a["facts"].get("trade_id")
                if trade_id is None:
                    continue
                m = messages[a["message"]]
                att = f"{sc.id}/{a['filename']}"
                g.node(m["message_id"], "email")
                g.node(att, "attachment", a["filename"])
                g.edge(m["message_id"], att, "attaches")
                g.edge(att, trade_id, "about")
        if sc.phase == "post_trade":
            for doc in sc.data["documents"]:
                g.node(doc["number"], "invoice")
                g.edge(doc["trade_id"], doc["number"], "billed")
                if doc["trade_id"] in payment_of:
                    pay = payment_of[doc["trade_id"]]
                    g.node(pay, "payment")
                    g.edge(doc["number"], pay, "settled_by")
        if sc.phase == "onboarding" and "london_entity" in sc.data.get("onboarded", {}):
            ob = sc.data["onboarded"]
            agr = f"agr:{ob['agreement']}"
            g.node(agr, "agreement", ob["agreement"])
            g.edge(ob["london_entity"], agr, "signed")
            g.edge(ob["parent_counterparty"], agr, "signed")
            g.edge(ob["entity"], agr, "acceded")
            g.edge(agr, ob["counterparty"], "sub_limit")
    return g


def _labels(g: Graph, nodes: Sequence[str]) -> list[str]:
    return sorted(g.labels[n] for n in nodes)


def _name(g: Graph, node: str) -> str:
    return g.labels[node]


class _Questions:
    """Collects questions; each ``add`` answers its own query on the graph."""

    def __init__(self, g: Graph, result: BuildResult) -> None:
        self.g = g
        self.result = result
        self.entries: list[dict[str, Any]] = []

    def _add(self, text: str, answer: Any, shape: str, hops: int | None, query: dict[str, Any]) -> None:
        self.entries.append({"question": text, "answer": answer, "shape": shape, "hops": hops, "query": query})

    def reach(
        self,
        text: str,
        start: str,
        kinds: Sequence[str],
        want: Sequence[str],
        direction: str = "out",
        *,
        ids: bool = False,
    ) -> None:
        dist = self.g.distances(start, kinds, direction)
        hit = {n: d for n, d in dist.items() if self.g.types[n] in want}
        if not hit:
            raise ValueError(f"empty reach answer for {text}")
        answer = sorted(hit) if ids else _labels(self.g, list(hit))
        query = {"op": "reach", "start": start, "kinds": list(kinds), "want": list(want), "direction": direction}
        self._add(text, answer, "reachable", max(hit.values()), query)

    def _unique_path(self, text: str, start: str, end: str, kinds: Sequence[str]) -> list[str]:
        paths = self.g.shortest_paths(start, end, kinds)
        if len(paths) != 1:
            raise ValueError(f"{len(paths)} shortest paths for {text}")
        return paths[0]

    def path(self, text: str, start: str, end: str, kinds: Sequence[str]) -> None:
        path = self._unique_path(text, start, end, kinds)
        query = {"op": "path", "start": start, "end": end, "kinds": list(kinds)}
        self._add(text, [self.g.labels[n] for n in path], "path", len(path) - 1, query)

    def hops(self, text: str, start: str, end: str, kinds: Sequence[str]) -> None:
        paths = self.g.shortest_paths(start, end, kinds)
        if not paths:
            raise ValueError(f"no path for {text}")
        query = {"op": "hops", "start": start, "end": end, "kinds": list(kinds)}
        self._add(text, len(paths[0]) - 1, "path_length", len(paths[0]) - 1, query)

    def exists(self, text: str, start: str, end: str, kinds: Sequence[str]) -> None:
        paths = self.g.shortest_paths(start, end, kinds)
        query = {"op": "exists", "start": start, "end": end, "kinds": list(kinds)}
        self._add(text, "yes" if paths else "no", "path_exists", len(paths[0]) - 1 if paths else None, query)

    def share(self, text: str, person: str, entity: str) -> None:
        pct = beneficial_owners(self.result.universe, entity, threshold=0)[person]
        hops = len(self._unique_path(text, person, entity, OWNERSHIP)) - 1
        self._add(text, pct, "effective_share", hops, {"op": "share", "start": person, "end": entity})


def _ownership_questions(q: _Questions, g: Graph) -> None:
    n = lambda node: _name(g, node)  # noqa: E731
    for target in ("cp-kestrel", "cp-tiberpeak", "cp-harbourline", "newco-hou"):
        q.reach(f"Which natural persons own {n(target)}, directly or through any chain of holding entities?",
                target, OWNERSHIP, ("person",), "in")  # fmt: skip
    for owner in (
        "hold-founders",
        "own-founder-1",
        "hold-infra",
        "own-tiber-hidden",
        "own-kestrel-pep",
        "hold-harbourline-grp",
    ):
        q.reach(f"Which legal entities does {n(owner)} own, directly or indirectly?",
                owner, OWNERSHIP, LEGAL_ENTITY_TYPES)  # fmt: skip
    for person, entity in (
        ("own-founder-1", "newco-hou"),
        ("own-founder-2", "newco-syd"),
        ("own-kestrel-pep", "cp-kestrel"),
        ("own-tiber-hidden", "cp-tiberpeak"),
    ):
        q.share(
            f"What effective percentage of {n(entity)} does {n(person)} hold through the ownership chain?",
            person,
            entity,
        )
    for start, end in (
        ("own-founder-1", "newco-lon"),
        ("own-kestrel-pep", "cp-kestrel"),
        ("own-harbourline-1", "cp-harbourline"),
        ("hold-infra", "newco-syd"),
        ("own-tiber-hidden", "hold-tiber-cy"),
    ):
        q.hops(f"How many ownership hops lie between {n(start)} and {n(end)}?", start, end, OWNERSHIP)
    for start, end in (
        ("own-tiber-hidden", "cp-tiberpeak"),
        ("own-kestrel-pep", "cp-kestrel"),
        ("own-founder-2", "newco-nyc"),
    ):
        q.path(f"List the ownership chain from {n(start)} down to {n(end)}, in order.", start, end, OWNERSHIP)
    q.exists(
        f"Does an ownership chain run from {n('own-kestrel-pep')} to {n('cp-kestrel')}?",
        "own-kestrel-pep",
        "cp-kestrel",
        OWNERSHIP,
    )
    q.exists(
        f"Does an ownership chain run from {n('own-kestrel-pep')} to {n('cp-tiberpeak')}?",
        "own-kestrel-pep",
        "cp-tiberpeak",
        OWNERSHIP,
    )


def _account_questions(q: _Questions, g: Graph) -> None:
    n = lambda node: _name(g, node)  # noqa: E731
    for org in ("newco-lon", "newco-hou", "cp-bp-au"):
        q.reach(f"Which banks hold accounts of {n(org)}?", org, ("has_account", "held_at"), ("bank",))
    for bank in ("bank-northgate", "bank-hudson"):
        q.reach(
            f"Which legal entities hold an account at {n(bank)}?",
            bank,
            ("has_account", "held_at"),
            LEGAL_ENTITY_TYPES,
            "in",
        )


def _lineage_questions(q: _Questions, g: Graph) -> None:
    result = q.result
    messages = {(sc.id, m["key"]): m for sc in result.scenarios for m in sc.messages}
    for sc in result.scenarios:
        if sc.phase != "post_trade":
            continue
        for doc in sc.data["documents"]:
            m = messages[(sc.id, doc["message"])]
            q.reach(
                f"Following email to attachment to trade to invoice to payment, which payment does the email "
                f"{m['message_id']} (subject: {m['subject']}), carrying {doc['filename']}, lead to?",
                m["message_id"], LINEAGE, ("payment",), ids=True,
            )  # fmt: skip


def _combined_questions(q: _Questions, g: Graph) -> None:
    n = lambda node: _name(g, node)  # noqa: E731
    q.reach(f"Which banks hold accounts of companies that {n('own-founder-1')} owns, directly or indirectly?",
            "own-founder-1", BANKING, ("bank",))  # fmt: skip
    q.reach(f"Which payments settle trades of companies that {n('own-kestrel-pep')} owns, directly or indirectly?",
            "own-kestrel-pep", EXPOSURE, ("payment",), ids=True)  # fmt: skip
    q.reach(f"Which payments settle trades of companies that {n('own-founder-1')} owns, directly or indirectly?",
            "own-founder-1", EXPOSURE, ("payment",), ids=True)  # fmt: skip
    pay = {r["scenario_id"]: r["payment_id"] for r in table_rows(q.result)["payments"]}
    q.path(f"List the chain from {n('own-kestrel-pep')} to the payment {pay['x2-london-kestrel-vlsfo']}, through ownership, trade and invoice.",
           "own-kestrel-pep", pay["x2-london-kestrel-vlsfo"], EXPOSURE)  # fmt: skip
    q.path(f"List the chain from {n('own-harbourline-1')} to the payment {pay['x3-sydney-harbourline-ulp']}, through ownership, trade and invoice.",
           "own-harbourline-1", pay["x3-sydney-harbourline-ulp"], EXPOSURE)  # fmt: skip


def _accession_questions(q: _Questions, g: Graph) -> None:
    n = lambda node: _name(g, node)  # noqa: E731
    agr = next(a for a, t in g.types.items() if t == "agreement")
    q.reach("Which NewCo entities sign or accede to the BP master agreement that NewCo Sydney acceded to?",
            agr, ("signed", "acceded"), ("newco",), "in")  # fmt: skip
    q.path(f"List the chain from {n('newco-syd')} to {n('cp-bp-au')} through the master agreement accession and sub-limit.",
           "newco-syd", "cp-bp-au", ACCESSION)  # fmt: skip


def traversal_questions(result: BuildResult) -> list[dict[str, Any]]:
    """Return the traversal questions in a fixed order, each with ``question``, ``answer``, ``shape``,
    ``hops`` (the deepest shortest-path distance the answer needs) and ``query``."""
    g = build_graph(result)
    q = _Questions(g, result)
    _ownership_questions(q, g)
    _account_questions(q, g)
    _lineage_questions(q, g)
    _combined_questions(q, g)
    _accession_questions(q, g)
    return q.entries


__all__ = ["Graph", "build_graph", "traversal_questions"]
