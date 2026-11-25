# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Turn each manifest's ``questions[]`` into ``golden_answers.json`` for the FEAT-003 harness.

``python -m tests.benchmark.fsi_commodity.golden`` rewrites the file; ``--check`` exits 1 when it differs
from a fresh build.

Format, as read by ``tests/benchmark/compare.py``::

    {"//format": "...", "<query_id>": {"expected": <result count>, "tolerance_pct": 0, "tolerance": 0,
                                       "type": "...", "question": "...", "answer": <ground truth>}}

``compare.py`` checks ``abs(len(results) - expected) <= tolerance``, so ``expected`` is the number of
results a correct answer holds (one per element of a list answer, one for a single value). The ground-truth
value itself is in ``answer``. ``tolerance_pct`` is the FEAT-003 spelling of the same tolerance (0 here).
Keys starting ``//`` are comments.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .build import build
from .structured import DATASOURCE_ID, NAMESPACE_ID, TABLES, table_rows
from .traversal import traversal_questions
from .universe import DEFAULT_SEED

if TYPE_CHECKING:
    from .build import BuildResult

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_answers.json"
QUESTION_TYPES = ("graph_traversal", "hybrid_retrieval", "reconciliation", "metadata")


def _result_count(answer: Any) -> int:
    return len(answer) if isinstance(answer, list) else 1


def _entry(qtype: str, question: str, answer: Any) -> dict[str, Any]:
    return {
        "expected": _result_count(answer),
        "tolerance_pct": 0,
        "tolerance": 0,
        "type": qtype,
        "question": question,
        "answer": answer,
    }


def _catalog_questions(result: BuildResult) -> dict[str, dict[str, Any]]:
    """Metadata lookups on the structured source (namespace, datasource, tables, rows)."""
    rows = table_rows(result)
    return {
        "catalog-q1": _entry("metadata", f"Which namespace holds the datasource {DATASOURCE_ID}?", NAMESPACE_ID),
        "catalog-q2": _entry(
            "metadata", f"Which tables does the datasource {DATASOURCE_ID} expose?", [t.name for t in TABLES]
        ),
        "catalog-q3": _entry("metadata", "How many rows does the deals table hold?", len(rows["deals"])),
    }


def _traversal_entries(result: BuildResult) -> dict[str, dict[str, Any]]:
    """Multi-hop traversal questions (see `traversal.py`), with their shape, depth and replayable query."""
    out: dict[str, dict[str, Any]] = {}
    for i, tq in enumerate(traversal_questions(result), start=1):
        entry = _entry("graph_traversal", tq["question"], tq["answer"])
        entry.update(shape=tq["shape"], hops=tq["hops"], query=tq["query"])
        out[f"graph-q{i:02d}"] = entry
    return out


def build_golden(result: BuildResult) -> dict[str, Any]:
    """Return the golden answers as an ordered dict: comment keys first, then query ids in build order."""
    out: dict[str, Any] = {
        "//format": (
            '{ "<query_id>": { "expected": <result count>, "tolerance_pct": 0, "tolerance": 0, "type": ..., '
            '"question": ..., "answer": ... } }. compare.py reads expected and tolerance (absolute count).'
        ),
        "//generated": (
            f"python -m tests.benchmark.fsi_commodity.golden (seed {result.seed}); one entry per manifest "
            "question plus traversal questions over the universe and scenario links (graph-qNN) "
            "and metadata questions on the structured source. Do not edit by hand."
        ),
    }
    for sc in result.scenarios:
        for q in sc.questions:
            out[q["id"]] = _entry(q["type"], q["question"], q["expected"])
    out.update(_traversal_entries(result))
    out.update(_catalog_questions(result))
    return out


def render_golden(result: BuildResult) -> bytes:
    """Return the file bytes: 2-space JSON, ``\\n`` line endings, trailing newline."""
    return (json.dumps(build_golden(result), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Write (or with ``--check``, verify) ``golden_answers.json``; returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.benchmark.fsi_commodity.golden", description="Regenerate golden_answers.json."
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the file differs from a fresh build")
    parser.add_argument("--out", type=Path, default=GOLDEN_PATH, help="output file (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    data = render_golden(build(args.seed))
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != data:
            print(f"changed: {args.out.name}")
            return 1
        print(f"{args.out.name} matches a fresh build")
        return 0
    args.out.write_bytes(data)
    print(f"wrote {args.out} ({len(json.loads(data)) - 2} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
