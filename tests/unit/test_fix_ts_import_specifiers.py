# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``scripts/fix-ts-import-specifiers.mjs``.

On Windows the Smithy TypeScript codegen writes some relative import specifiers
with backslashes (``"./src\\pagination\\Interfaces"``) or relative to the
package root instead of the importing file, and ``pnpm run build`` fails with
TS2307. ``smithy-generate.sh`` runs the helper on both clients before building
them. These tests run it on small fake packages: it must repair exactly those
specifiers, leave correct output byte-for-byte alone (Linux and macOS), and
fail when a specifier cannot be resolved.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "fix-ts-import-specifiers.mjs"
_NODE = shutil.which("node")

if _NODE is None:
    pytest.skip("node is not installed", allow_module_level=True)


def _package(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "client"
    for rel, text in {"package.json": "{}\n", **files}.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_bytes(text.encode())
    return pkg


def _run(*dirs: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_NODE), str(_SCRIPT), *map(str, dirs)], capture_output=True, text=True, encoding="utf-8", check=False
    )


# What the codegen emitted on Windows (Smithy TypeScript codegen 0.49.1).
_WINDOWS_OUTPUT = {
    "src/pagination/Interfaces.ts": "export interface P {}\n",
    "src/pagination/ListMetricsPaginator.ts": "export const p = 1;\n",
    "src/pagination/index.ts": (
        'export * from "./src\\pagination\\Interfaces";\nexport * from "./src\\pagination\\ListMetricsPaginator";\n'
    ),
    "src/models/errors.ts": "export class E {}\n",
    "src/models/ServiceException.ts": "export class S {}\n",
    "src/schemas/schemas_0.ts": (
        'import { S } from "..\\models\\ServiceException";\n'
        'import {\n  E,\n} from "..\\models\\errors";\n'
        'import { x } from "@smithy/core";\n'
    ),
}


def test_windows_codegen_output_is_repaired(tmp_path: Path) -> None:
    pkg = _package(tmp_path, _WINDOWS_OUTPUT)
    result = _run(pkg)
    assert result.returncode == 0, result.stderr
    assert (pkg / "src/pagination/index.ts").read_text() == (
        'export * from "./Interfaces";\nexport * from "./ListMetricsPaginator";\n'
    )
    assert (pkg / "src/schemas/schemas_0.ts").read_text() == (
        'import { S } from "../models/ServiceException";\n'
        'import {\n  E,\n} from "../models/errors";\n'
        'import { x } from "@smithy/core";\n'
    )
    assert "Fixed import specifiers in 2 file(s)" in result.stdout  # only the two broken files


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    pkg = _package(tmp_path, _WINDOWS_OUTPUT)
    assert _run(pkg).returncode == 0
    before = {p: p.read_bytes() for p in pkg.rglob("*.ts")}
    result = _run(pkg)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert {p: p.read_bytes() for p in pkg.rglob("*.ts")} == before


def test_correct_output_is_left_untouched(tmp_path: Path) -> None:
    """Linux and macOS output: every specifier resolves, so no file is written."""
    files = {
        "src/index.ts": 'export * from "./models";\nexport * from "./pagination";\nimport "./runtimeConfig";\n',
        "src/models/index.ts": 'export * from "./errors";\n',
        "src/models/errors.ts": "export class E {}\r\n",
        "src/pagination/index.ts": 'export * from "./Interfaces";\nconst m = await import("../models/errors");\n',
        "src/pagination/Interfaces.ts": "export interface P {}\n",
        "src/runtimeConfig.ts": "export {};\n",
        "src/client.ts": "const c = require('./runtimeConfig');\nimport { a } from \"@aws-sdk/core\";\n",
    }
    pkg = _package(tmp_path, files)
    mtimes = {p: p.stat().st_mtime_ns for p in pkg.rglob("*.ts")}
    result = _run(pkg)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    for rel, text in files.items():
        assert (pkg / rel).read_bytes() == text.encode()
    assert {p: p.stat().st_mtime_ns for p in pkg.rglob("*.ts")} == mtimes


def test_an_unresolvable_specifier_fails_loudly(tmp_path: Path) -> None:
    pkg = _package(tmp_path, {"src/index.ts": 'export * from "./missing";\n'})
    result = _run(pkg)
    assert result.returncode == 1
    assert "src/index.ts: ./missing" in result.stderr
    assert (pkg / "src/index.ts").read_text() == 'export * from "./missing";\n'


def test_node_modules_is_not_scanned(tmp_path: Path) -> None:
    pkg = _package(tmp_path, {"node_modules/dep/index.ts": 'export * from "./not-here";\n'})
    assert _run(pkg).returncode == 0


def test_a_package_without_sources_is_a_no_op(tmp_path: Path) -> None:
    pkg = _package(tmp_path, {})
    result = _run(pkg)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_no_arguments_is_a_usage_error() -> None:
    assert _run().returncode == 2
