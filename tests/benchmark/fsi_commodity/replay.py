# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline replay: walk the committed corpus in time order and compare it with each manifest's ground truth.

``python -m tests.benchmark.fsi_commodity.replay [--corpus DIR] [--phase PHASE]``. No network, no AWS, no
model. For every phase it prints the messages replayed, the steward ``review_events`` applied at their
points, and expected (manifest) against observed (files on disk) counts and attachment hashes. Exit 1 when
any expected value differs from what is on disk.
"""

from __future__ import annotations

import argparse
import email
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from email import policy
from pathlib import Path
from typing import Any

from .build import CORPUS_DIR
from .writers.manifest import sha256_hex

PHASE_ORDER = ("onboarding", "trade", "post-trade", "settlement", "docsets", "lifecycle", "execution")


@dataclass
class PhaseReport:
    """Expected and observed figures for one phase."""

    phase: str
    scenarios: int = 0
    expected: dict[str, int] = field(default_factory=lambda: {"emails": 0, "attachments": 0, "chats": 0})
    observed: dict[str, int] = field(default_factory=lambda: {"emails": 0, "attachments": 0, "chats": 0})
    events_applied: int = 0
    contradictions: int = 0
    caught: int = 0
    problems: list[str] = field(default_factory=list)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _timeline(manifests: Sequence[dict[str, Any]]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Return ``(timestamp, scenario, kind, record)`` for every message and chat line, sorted by time."""
    items: list[tuple[str, str, str, dict[str, Any]]] = []
    for m in manifests:
        for msg in m["messages"]:
            items.append((msg["timestamp"], m["scenario"], "email", msg))
        for chat in m["chats"]:
            for line in chat["lines"]:
                items.append((line["timestamp"], m["scenario"], "chat", {"key": chat["key"], **line}))
    return sorted(items, key=lambda i: (i[0], i[1], i[2]))


def replay_phase(corpus: Path, phase: str) -> PhaseReport:
    """Replay one phase folder and return the report."""
    report = PhaseReport(phase)
    base = corpus / phase
    manifests = [_load(p) for p in sorted(base.glob("*/manifest.json"))] if base.is_dir() else []
    report.scenarios = len(manifests)
    pending = {m["scenario"]: list(m["review_events"]) for m in manifests}

    for _stamp, scenario, kind, rec in _timeline(manifests):
        if kind != "email":
            continue
        # Apply the steward corrections that follow this message.
        for ev in [e for e in pending[scenario] if e["after_message"] == rec["key"]]:
            pending[scenario].remove(ev)
            report.events_applied += 1

    for m in manifests:
        sid = m["scenario"]
        root = base / sid
        report.expected["emails"] += m["counts"]["emails"]
        report.expected["attachments"] += m["counts"]["attachments"]
        report.expected["chats"] += m["counts"]["chats"]
        for left in pending[sid]:
            report.problems.append(f"{sid}: review event after unknown message {left['after_message']!r}")
        for msg in m["messages"]:
            eml = root / msg["files"]["eml"]
            if not eml.is_file() or not (root / msg["files"]["md"]).is_file():
                report.problems.append(f"{sid}: missing files for message {msg['key']}")
                continue
            parsed = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
            if parsed["Message-ID"] != msg["message_id"]:
                report.problems.append(f"{sid}: Message-ID differs for {msg['key']}")
            report.observed["emails"] += 1
        for att in m["attachments"]:
            path = root / att["path"]
            if not path.is_file():
                report.problems.append(f"{sid}: missing attachment {att['path']}")
            elif sha256_hex(path.read_bytes()) != att["sha256"]:
                report.problems.append(f"{sid}: SHA-256 differs for {att['path']}")
            else:
                report.observed["attachments"] += 1
        for chat in m["chats"]:
            if (root / chat["file"]).is_file():
                report.observed["chats"] += 1
            else:
                report.problems.append(f"{sid}: missing chat file {chat['file']}")
        for c in m["expected_contradictions"]:
            report.contradictions += 1
            report.caught += bool(c.get("caught"))
    if report.expected != report.observed:
        report.problems.append(f"expected {report.expected} but observed {report.observed}")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Run the replay; returns 0 when expected and observed agree in every phase."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.benchmark.fsi_commodity.replay",
        description="Replay the corpus offline, in time order, against its ground truth.",
    )
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR, help="corpus directory (default: %(default)s)")
    parser.add_argument("--phase", choices=PHASE_ORDER, help="replay one phase only")
    args = parser.parse_args(argv)

    phases = (args.phase,) if args.phase else PHASE_ORDER
    reports = [replay_phase(args.corpus, p) for p in phases]
    print("phase        scenarios  emails (exp/obs)  attachments  chats  review events  contradictions (caught)")
    for r in reports:
        print(
            f"{r.phase:<12} {r.scenarios:>9}  {r.expected['emails']:>6}/{r.observed['emails']:<9} "
            f"{r.expected['attachments']:>4}/{r.observed['attachments']:<6} "
            f"{r.expected['chats']:>3}/{r.observed['chats']:<3} {r.events_applied:>13}  "
            f"{r.contradictions:>14} ({r.caught})"
        )
    totals = {k: sum(r.observed[k] for r in reports) for k in ("emails", "attachments", "chats")}
    print(f"observed totals: {totals['emails']} emails, {totals['attachments']} attachments, {totals['chats']} chats")
    problems = [p for r in reports for p in r.problems]
    for p in problems:
        print(f"PROBLEM: {p}")
    if not any(r.scenarios for r in reports):
        print(f"PROBLEM: no manifests under {args.corpus}")
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
