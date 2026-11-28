# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contract corpus: derivatives are reproducible, consistent with the universe, and large documents stay deferred."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.benchmark.fsi_commodity.contracts import derive

ROOT = Path(derive.HERE)
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))["documents"]
CASES = {k: v for k, v in json.loads((ROOT / "cases.json").read_text(encoding="utf-8")).items() if not k.startswith("//")}


def test_derivatives_match_a_fresh_build():
    for rel, data in derive.build().items():
        assert (ROOT / rel).read_bytes() == data, rel


def test_every_document_records_pages_size_and_class():
    for d in CATALOG:
        assert d["size_class"] in ("small", "large") or d["status"] != "ok", d["id"]
        if d.get("status", "ok") == "ok":
            assert d["pages"] and d["bytes"] and len(d["sha256"]) == 64, d["id"]


def test_derivative_files_match_catalog():
    for d in (x for x in CATALOG if x["kind"] == "derivative"):
        data = (ROOT / d["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == d["sha256"]
        assert len(data) == d["bytes"]


def test_large_documents_are_deferred_and_credit_agreement_is_large():
    by_id = {d["id"]: d for d in CATALOG}
    assert by_id["src02"]["size_class"] == "large" and by_id["src02"]["local_pages"] == 287
    for d in CATALOG:
        if d["size_class"] != "small":
            assert d["ingest"] == "deferred", d["id"]
    for case in CASES.values():
        assert case["doc"] in by_id or case["doc"] in {d["id"] for d in CATALOG}


def test_derivative_parties_exist_in_universe():
    orgs = json.loads(derive.UNIVERSE.read_text(encoding="utf-8"))["orgs"]
    for d in (x for x in CATALOG if x["kind"] == "derivative"):
        assert set(d["parties"]) <= set(orgs), d["id"]


def test_derivative_answers_appear_in_the_document_text():
    import pypdfium2

    texts = {}
    for d in (x for x in CATALOG if x["kind"] == "derivative"):
        pdf = pypdfium2.PdfDocument((ROOT / d["file"]).read_bytes())
        texts[d["id"]] = " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    for key, c in CASES.items():
        if c["origin"] != "derivative" or not isinstance(c["answer"], str):
            continue
        assert c["answer"] in " ".join(texts[c["doc"]].split()), key


def test_as_is_cases_only_use_sources_in_the_list():
    ids = {s["id"] for s in json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]}
    assert {c["doc"] for c in CASES.values() if c["origin"] == "as_is"} <= ids


def test_originals_are_not_tracked():
    out = subprocess.run(["git", "ls-files", str(ROOT / "originals")], capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    assert out.stdout.strip() == ""
