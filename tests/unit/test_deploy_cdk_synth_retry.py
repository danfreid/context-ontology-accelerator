# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``scripts/lib/cdk-synth.sh`` (the synth step of ``deploy.sh``).

On Windows, CDK asset staging intermittently fails to rename
``asset.<hash>-building`` to ``asset.<hash>`` with EPERM; finished bundles are
cached, so a re-run succeeds. ``coa_cdk_synth`` retries that failure only under
Git Bash / MSYS2 / Cygwin, and runs the command exactly once everywhere else.
A ``uname`` shell function picks the platform and a stub synth fails a set number of times.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_LIB = _ROOT / "scripts" / "lib" / "cdk-synth.sh"
_BASH = shutil.which("bash") or "/bin/bash"
_EPERM = (
    "EPERM: operation not permitted, rename 'infra\\\\cdk.out\\\\asset.3f2a9c-building' "
    "-> 'infra\\\\cdk.out\\\\asset.3f2a9c'"
)


def _run(tmp_path: Path, system: str, failures: int, message: str = _EPERM) -> tuple[int, str, int]:
    """Return (exit status, stderr, number of synth runs)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    count = tmp_path / "count"
    (bin_dir / "fake-synth").write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(( $(cat "{count.as_posix()}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{count.as_posix()}"\n'
        'echo "synth stdout"\n'
        f'if [ "$n" -le {failures} ]; then echo "{message}" >&2; exit 3; fi\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    # A function, not a PATH stub: Git Bash puts its own /usr/bin ahead of PATH.
    script = (
        f'set -euo pipefail\nuname() {{ echo "{system}"; }}\n'
        f'source "{_LIB.as_posix()}"\ncoa_cdk_synth fake-synth >/dev/null\n'
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run([_BASH, "-c", script], env=env, capture_output=True, text=True, check=False)
    runs = int(count.read_text()) if count.exists() else 0
    return result.returncode, result.stderr, runs


@pytest.mark.parametrize("system", ["MINGW64_NT-10.0-26100", "MSYS_NT-10.0", "CYGWIN_NT-10.0"])
def test_windows_retries_the_eperm_rename_until_synth_succeeds(tmp_path: Path, system: str) -> None:
    rc, err, runs = _run(tmp_path, system, failures=2)
    assert (rc, runs) == (0, 3), err
    assert err.count("retrying") == 2


def test_windows_gives_up_after_five_attempts(tmp_path: Path) -> None:
    rc, _, runs = _run(tmp_path, "MINGW64_NT-10.0", failures=10)
    assert (rc, runs) == (3, 5)


def test_windows_does_not_retry_other_failures(tmp_path: Path) -> None:
    rc, err, runs = _run(tmp_path, "MINGW64_NT-10.0", failures=1, message="Error: something else broke")
    assert (rc, runs) == (3, 1)
    assert "something else broke" in err
    assert "retrying" not in err


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_linux_and_macos_run_synth_once(tmp_path: Path, system: str) -> None:
    rc, err, runs = _run(tmp_path, system, failures=1)
    assert (rc, runs) == (3, 1)
    assert "EPERM" in err
    assert "retrying" not in err


@pytest.mark.parametrize("system", ["Linux", "MINGW64_NT-10.0"])
def test_a_passing_synth_runs_once_and_keeps_stdout_redirected(tmp_path: Path, system: str) -> None:
    rc, err, runs = _run(tmp_path, system, failures=0)
    assert (rc, runs, err) == (0, 1, "")


def test_deploy_runs_synth_through_the_helper() -> None:
    deploy = (_ROOT / "scripts" / "deploy.sh").read_text()
    assert 'source "$REPO_ROOT/scripts/lib/cdk-synth.sh"' in deploy
    assert 'coa_cdk_synth pnpm --filter coa-infra exec cdk synth "${CONTEXT[@]}" >/dev/null' in deploy
