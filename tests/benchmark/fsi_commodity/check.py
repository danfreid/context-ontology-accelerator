# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""``--check``: compare a fresh build with the committed corpus byte for byte."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from .scenario import PHASE_DIRS


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def diff_corpus(files: Mapping[str, bytes], corpus: Path, partial: bool = False) -> dict[str, list[str]]:
    """Return ``{"missing": [...], "extra": [...], "changed": [...]}`` as sorted relative paths.

    Extras are looked for only in the trees the generator owns: the phase folders and the top-level
    entries present in ``files``. With ``partial`` (a one-scenario build), extras are looked for only
    inside the scenario folders present in ``files``.
    """
    if partial:
        owned = {"/".join(rel.split("/")[:2]) if rel.count("/") else rel for rel in files}
    else:
        owned = set(PHASE_DIRS.values()) | {rel.split("/", 1)[0] for rel in files}
    on_disk: dict[str, Path] = {}
    for top in sorted(owned):
        base = corpus / top
        if base.is_file():
            on_disk[top] = base
        elif base.is_dir():
            on_disk.update({p.relative_to(corpus).as_posix(): p for p in base.rglob("*") if p.is_file()})
    missing = sorted(set(files) - set(on_disk))
    extra = sorted(set(on_disk) - set(files))
    changed = sorted(r for r in set(files) & set(on_disk) if _digest(files[r]) != _digest(on_disk[r].read_bytes()))
    return {"missing": missing, "extra": extra, "changed": changed}
