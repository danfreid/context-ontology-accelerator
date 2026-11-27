# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Docsets phase: mixed-format and merged document sets, their page splits, and the reconciliation ground truth.

Every check reads the built files (PDF pages through pdfium, XLSX and DOCX through their XML) and compares them
with the manifest, so the ground truth an intake pipeline is scored against is shown to match the bytes.
"""

from __future__ import annotations

import copy
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from tests.benchmark.fsi_commodity import build as build_mod
from tests.benchmark.fsi_commodity.docsets.common import reconcile
from tests.benchmark.fsi_commodity.golden import build_golden
from tests.benchmark.fsi_commodity.scenario import Scenario
from tests.benchmark.fsi_commodity.writers.pdf_tools import BLANK, merge, scan
from tests.benchmark.fsi_commodity.writers.pdf_writer import Text, build_pdf
from tests.benchmark.fsi_commodity.writers.xlsx_writer import Sheet, build_xlsx, column

pytestmark = pytest.mark.unit

D1, D2 = "d1-houston-vitol-products-batched", "d2-london-bp-products-merged"
_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@pytest.fixture(scope="module")
def result() -> build_mod.BuildResult:
    return build_mod.build()


@pytest.fixture(scope="module")
def sc(result: build_mod.BuildResult) -> dict[str, Scenario]:
    return {s.id: s for s in result.scenarios if s.phase == "docsets"}


def _files(s: Scenario) -> dict[str, bytes]:
    return s.files()


def _att_bytes(s: Scenario, filename: str) -> bytes:
    att = next(a for a in s.attachments if a["filename"] == filename)
    return _files(s)[att["path"]]


def _pages(pdf: bytes) -> list[tuple[str, list[int]]]:
    doc = pdfium.PdfDocument(pdf)
    return [(doc[i].get_textpage().get_text_range(), [o.type for o in doc[i].get_objects()]) for i in range(len(doc))]


def _xlsx(content: bytes) -> dict[str, list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.iter(f"{_SS}sheet")]
        out = {}
        for i, name in enumerate(names, start=1):
            root = ET.fromstring(zf.read(f"xl/worksheets/sheet{i}.xml"))
            rows = []
            for r in root.iter(f"{_SS}row"):
                cells = []
                for c in r.iter(f"{_SS}c"):
                    t = c.find(f"{_SS}is/{_SS}t")
                    v = c.find(f"{_SS}v")
                    cells.append((t.text if t is not None else v.text if v is not None else "") or "")
                rows.append(cells)
            out[str(name)] = rows
    return out


def _docx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p"))


def _docset(s: Scenario) -> dict[str, Any]:
    (ds,) = s.docsets
    return ds


# --- shape --------------------------------------------------------------------------------------------
def test_phase_has_its_own_counts_and_is_registered_after_settlement(
    result: build_mod.BuildResult, sc: dict[str, Scenario]
) -> None:
    assert build_mod.PHASES[-1] == "docsets"
    assert [s.id for s in result.scenarios if s.phase == "docsets"] == [D1, D2]
    totals = tuple(sum(s.counts[k] for s in sc.values()) for k in ("emails", "attachments", "chats"))
    assert totals == build_mod.EXPECTED_TOTALS["docsets"]


def test_build_is_byte_identical(sc: dict[str, Scenario]) -> None:
    again = {s.id: s for s in build_mod.build().scenarios if s.phase == "docsets"}
    for sid, s in sc.items():
        assert _files(s) == _files(again[sid])


def test_every_logical_document_sits_in_exactly_one_file_without_overlapping_pages(sc: dict[str, Scenario]) -> None:
    for s in sc.values():
        ds = _docset(s)
        ids = [d["doc_id"] for d in ds["documents"]]
        assert len(ids) == len(set(ids))
        for f in ds["files"]:
            att = next(a for a in s.attachments if a["filename"] == f["file"])
            assert att["facts"]["documents"] == f["documents"]
            if f["format"] == "pdf":
                numbers = [p["page"] for p in f["pages"]]
                assert numbers == list(range(1, f["page_count"] + 1))
                assert len(_pages(_att_bytes(s, f["file"]))) == f["page_count"]
                for doc in (d for d in ds["documents"] if d["file"] == f["file"]):
                    lo, hi = doc["page_range"]
                    assert all(p["doc_id"] == doc["doc_id"] for p in f["pages"][lo - 1 : hi])


# --- formats ------------------------------------------------------------------------------------------
def test_d1_mixes_docx_text_pdf_scanned_pdf_and_xlsx_from_three_senders(sc: dict[str, Scenario]) -> None:
    ds = _docset(sc[D1])
    assert {d["format"] for d in ds["documents"]} == {"docx", "pdf", "pdf-scan", "xlsx"}
    external = {d["source"] for d in ds["documents"]} - {sc[D1].messages[0]["from"]["address"]}
    assert len(external) == 4  # ship's agent, inspector, Vitol ops, Vitol settlements (NewCo sent the DOCX)
    assert {m["direction"] for m in sc[D1].messages} == {"outbound", "inbound"}


def test_scanned_pages_have_no_text_layer_and_one_image(sc: dict[str, Scenario]) -> None:
    seen = 0
    for s in sc.values():
        for f in _docset(s)["files"]:
            if f["format"] != "pdf":
                continue
            pages = _pages(_att_bytes(s, f["file"]))
            for meta, (text, objects) in zip(f["pages"], pages, strict=True):
                if meta["scanned"]:
                    seen += 1
                    assert text.strip() == ""
                    assert objects == [pdfium_c.FPDF_PAGEOBJ_IMAGE]
                elif meta["kind"] == "blank":
                    assert text.strip() == "" and objects == []
                else:
                    assert text.strip(), (f["file"], meta)
    assert seen >= 10


def test_bill_of_lading_pages_carry_their_original_and_copy_marks(sc: dict[str, Scenario]) -> None:
    for s in sc.values():
        for doc in (d for d in _docset(s)["documents"] if d["kind"] == "bill-of-lading" and d["format"] == "pdf"):
            pages = _pages(_att_bytes(s, doc["file"]))[doc["page_range"][0] - 1 : doc["page_range"][1]]
            marks = [f"ORIGINAL {n} of {doc['originals_issued']}" for n in doc["originals"]] + [
                "COPY - NOT NEGOTIABLE"
            ] * doc["copies"]
            assert len(pages) == len(marks)
            for (text, _), mark in zip(pages, marks, strict=True):
                assert mark in text and doc["number"] in text


def test_d2_is_one_merged_pdf_with_blank_separators_in_some_places_only(sc: dict[str, Scenario]) -> None:
    (merged,) = [f for f in _docset(sc[D2])["files"] if f["format"] == "pdf"]
    kinds = [p["kind"] for p in merged["pages"]]
    blanks = [i for i, k in enumerate(kinds) if k == "blank"]
    assert len(blanks) == 3
    # the ULSD and jet B/Ls touch without a separator; the confirmation is followed by one
    bl_pages = [p for p in merged["pages"] if p["doc_id"] in ("bl-ulsd", "bl-jet")]
    assert [p["page"] for p in bl_pages] == list(range(bl_pages[0]["page"], bl_pages[-1]["page"] + 1))
    assert kinds[1] == "blank"
    assert len(merged["documents"]) == 12


def test_xlsx_workbooks_match_the_deal(sc: dict[str, Scenario]) -> None:
    for s in sc.values():
        ds = _docset(s)
        products = {p["name"]: p for p in ds["deal"]["products"]}
        wb_file = next(f["file"] for f in ds["files"] if f["file"].startswith("pricing-"))
        sheets = _xlsx(_att_bytes(s, wb_file))
        summary = sheets["Summary"]
        assert summary[0][:3] == ["Product", "Pricing", "Index"]
        for row in summary[1:]:
            p = products[row[0]]
            assert Decimal(row[-2]) == Decimal(str(p["final_price"]))
            if p["price_type"] == "floating":
                quotes = sheets[p["name"][:31]][1:]
                assert [(d, Decimal(v)) for d, v in quotes] == [
                    (q["date"], Decimal(str(q["price"]))) for q in p["quotes"]
                ]
    rep = _xlsx(_att_bytes(sc[D1], "load-port-quantities.xlsx"))["Quantities"]
    bl = {
        d["products"][0]: d["quantities"][d["products"][0]]
        for d in _docset(sc[D1])["documents"]
        if d["kind"] == "bill-of-lading"
    }
    assert [Decimal(r[2]) for r in rep[1:]] == [Decimal(str(bl["ulsd"])), Decimal(str(bl["rbob"]))]


def test_docx_confirmation_and_loi_state_the_ranges_and_bl_quantities(sc: dict[str, Scenario]) -> None:
    ds = _docset(sc[D1])
    conf = _docx_text(_att_bytes(sc[D1], "NC-HOU-26-0101-confirmation.docx"))
    assert "3/3 original bills of lading" in conf
    for p in ds["deal"]["products"]:
        assert f"{p['quantity_min']:,.3f} to {p['quantity_max']:,.3f} {p['unit']}" in conf
    loi_doc = next(d for d in ds["documents"] if d["kind"] == "letter-of-indemnity")
    loi = _docx_text(_att_bytes(sc[D1], loi_doc["file"]))
    for key, number in loi_doc["bl_numbers"].items():
        assert number in loi and f"{loi_doc['quantities'][key]:,.3f}" in loi


# --- reconciliation -------------------------------------------------------------------------------------
def test_reconciliation_recomputes_from_the_manifest(sc: dict[str, Scenario]) -> None:
    for s in sc.values():
        manifest = s.manifest()
        (ds,) = manifest["docsets"]
        assert reconcile(copy.deepcopy(ds)) == ds["reconciliation"]


def test_floating_prices_follow_the_confirmation_formula(sc: dict[str, Scenario]) -> None:
    for s in sc.values():
        for p in _docset(s)["deal"]["products"]:
            if p["price_type"] == "floating":
                avg = sum((Decimal(str(q["price"])) for q in p["quotes"]), Decimal(0)) / len(p["quotes"])
                expected = (avg + Decimal(str(p["differential"]))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
                assert Decimal(str(p["final_price"])) == expected
                assert len(p["quotes"]) == 5


def test_d1_is_the_clean_control_case(sc: dict[str, Scenario]) -> None:
    rec = _docset(sc[D1])["reconciliation"]
    assert rec["result"] == "pass" and rec["exceptions"] == []
    assert all(c["ok"] for c in rec["counts"])
    assert all(q["within_range"] and q["documents_agree"] for q in rec["quantities"])
    assert all(p["ok"] for p in rec["prices"]) and rec["invoice_arithmetic_ok"]
    assert sc[D1].expected_contradictions == []


def test_d2_finds_exactly_the_two_planted_problems(sc: dict[str, Scenario]) -> None:
    rec = _docset(sc[D2])["reconciliation"]
    assert rec["result"] == "fail"
    assert rec["exceptions"] == [
        "bill-of-lading originals for Jet A-1: 2 of 3",
        "invoice quantity for Eurobob gasoline 15000 mt differs from the B/L quantity 15902.004 mt",
    ]
    failed = [(c["kind"], c["product"], c["unit"]) for c in rec["counts"] if not c["ok"]]
    assert failed == [("bill-of-lading", "jet", "originals")]
    assert all(q["within_range"] for q in rec["quantities"])
    assert all(p["ok"] for p in rec["prices"]) and rec["invoice_arithmetic_ok"]
    assert [c["id"].split(":")[1] for c in sc[D2].expected_contradictions] == [
        "missing-original-bl",
        "invoice-quantity-not-bl",
    ]
    assert not any(c["caught"] for c in sc[D2].expected_contradictions)


def test_planted_invoice_quantity_is_printed_in_the_merged_pdf(sc: dict[str, Scenario]) -> None:
    ds = _docset(sc[D2])
    inv = next(d for d in ds["documents"] if d["kind"] == "invoice")
    text = _pages(_att_bytes(sc[D2], inv["file"]))[inv["page_range"][0] - 1][0]
    assert "15,000" in text and "15,902" not in text
    amounts = {k: Decimal(str(v)) for k, v in inv["amounts"].items()}
    assert sum(amounts.values()) == Decimal(str(inv["total"]))


def test_docset_questions_reach_the_golden_answers(result: build_mod.BuildResult) -> None:
    golden = build_golden(result)
    for sid in (D1, D2):
        assert {f"{sid}-q{i}" for i in range(1, 7)} <= set(golden)
    assert golden[f"{D2}-q3"]["answer"] == 23 and golden[f"{D2}-q5"]["answer"] == 12


# --- writers --------------------------------------------------------------------------------------------
def test_merge_pins_the_id_and_dates_and_reports_page_ranges() -> None:
    a, b = build_pdf("A", [Text("one")]), build_pdf("B", [Text("two")])
    first, ranges = merge([a, BLANK, b])
    second, _ = merge([a, BLANK, b])
    assert first == second and ranges == [(1, 1), (2, 2), (3, 3)]
    assert re.search(rb"/CreationDate\(D:20000101000000", first)


def test_scan_is_deterministic_per_seed_and_has_no_text() -> None:
    pdf = build_pdf("Scan", [Text("certificate of quality")])
    assert scan(pdf, 1) == scan(pdf, 1) != scan(pdf, 2)
    assert _pages(scan(pdf, 1)) == [("", [pdfium_c.FPDF_PAGEOBJ_IMAGE])]


def test_xlsx_writer_round_trips_cells_and_rejects_bad_sheets() -> None:
    from datetime import UTC, datetime

    when = datetime(2026, 1, 2, tzinfo=UTC)
    data = build_xlsx([Sheet("S", [["a", 1, Decimal("2.50"), None, "b"]])], title="t", author="x", created=when)
    assert data == build_xlsx([Sheet("S", [["a", 1, Decimal("2.50"), None, "b"]])], title="t", author="x", created=when)
    assert _xlsx(data)["S"] == [["a", "1", "2.50", "b"]]
    assert [column(i) for i in (0, 25, 26, 701)] == ["A", "Z", "AA", "ZZ"]
    with pytest.raises(ValueError):
        build_xlsx([Sheet("S", []), Sheet("S", [])], title="t", author="x", created=when)
