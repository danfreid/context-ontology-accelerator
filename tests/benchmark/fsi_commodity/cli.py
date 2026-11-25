# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``python -m tests.benchmark.fsi_commodity [--scenario ID] [--check] [--out DIR] [--seed N]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .build import CORPUS_DIR, BuildError, build, render_files, write_files
from .check import diff_corpus
from .golden import GOLDEN_PATH, render_golden
from .universe import DEFAULT_SEED


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.benchmark.fsi_commodity",
        description="Build the NewCo commodity-trading corpus. No network or AWS calls.",
    )
    parser.add_argument(
        "--scenario", metavar="ID", help="write one scenario (plus universe.json), e.g. s1-houston-vitol-wti"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the corpus on disk differs from a fresh build")
    parser.add_argument("--out", type=Path, default=CORPUS_DIR, help="corpus directory (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="corpus seed (default: %(default)s)")
    args = parser.parse_args(argv)

    try:
        result = build(args.seed)
        files = render_files(result, only=args.scenario)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2

    if args.check:
        diff = diff_corpus(files, args.out, partial=args.scenario is not None)
        for label in ("missing", "extra", "changed"):
            for rel in diff[label]:
                print(f"{label}: {rel}")
        golden_ok = True
        if args.scenario is None and args.out == CORPUS_DIR:
            golden_ok = GOLDEN_PATH.is_file() and GOLDEN_PATH.read_bytes() == render_golden(result)
            if not golden_ok:
                print(f"changed: {GOLDEN_PATH.name}")
        if any(diff.values()) or not golden_ok:
            return 1
        print(f"corpus matches a fresh build ({len(files)} files)")
        return 0

    write_files(files, args.out)
    if args.scenario is None and args.out == CORPUS_DIR:
        GOLDEN_PATH.write_bytes(render_golden(result))
    print(f"wrote {len(files)} files to {args.out}")
    return 0
