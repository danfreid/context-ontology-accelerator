# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle phase: one master agreement from draft to the calculations that use its terms.

The checks read the built mailbox (EML headers and bodies, DOCX tracked changes and comments, the scanned PDF)
and recompute every "as of" answer and calculation from the manifest's event timeline alone.
"""

from __future__ import annotations

import email
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import policy
from typing import Any

import pypdfium2 as pdfium
import pytest

from tests.benchmark.fsi_commodity import build as build_mod
from tests.benchmark.fsi_commodity.golden import build_golden
from tests.benchmark.fsi_commodity.lifecycle.common import (
    late_interest,
    margin_call,
    net_settlement,
    renewal_notice_deadline,
    terms_as_of,
    value_as_of,
)
from tests.benchmark.fsi_commodity.lifecycle.l1_london_shell_mpta import FILE, SCENARIO_ID
from tests.benchmark.fsi_commodity.scenario import Scenario
from tests.benchmark.fsi_commodity.writers.docx_writer import Comment, Para, Run, build_docx, plain

pytestmark = pytest.mark.unit
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@pytest.fixture(scope="module")
def sc() -> Scenario:
    return next(s for s in build_mod.build().scenarios if s.phase == "lifecycle")


@pytest.fixture(scope="module")
def files(sc: Scenario) -> dict[str, bytes]:
    return sc.files()


@pytest.fixture(scope="module")
def manifest(sc: Scenario) -> dict[str, Any]:
    return sc.manifest()


def _msg(m: dict[str, Any], key: str) -> dict[str, Any]:
    return next(x for x in m["messages"] if x["key"] == key)


def _eml(files: dict[str, bytes], m: dict[str, Any], key: str) -> email.message.EmailMessage:
    return email.message_from_bytes(files[_msg(m, key)["files"]["eml"]], policy=policy.default)  # type: ignore[return-value]


def _body(files: dict[str, bytes], m: dict[str, Any], key: str) -> str:
    part = _eml(files, m, key).get_body(("plain",))
    assert part is not None
    return part.get_content()


def _att(files: dict[str, bytes], m: dict[str, Any], key: str, filename: str) -> bytes:
    a = next(x for x in m["attachments"] if x["message"] == key and x["filename"] == filename)
    return files[a["path"]]


def _docx(content: bytes) -> tuple[ET.Element, ET.Element | None]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        doc = ET.fromstring(zf.read("word/document.xml"))
        comments = ET.fromstring(zf.read("word/comments.xml")) if "word/comments.xml" in zf.namelist() else None
    return doc, comments


def _when(m: dict[str, Any], key: str) -> datetime:
    return datetime.fromisoformat(_msg(m, key)["timestamp"])


def _answer(m: dict[str, Any], n: int) -> Any:
    return next(q["expected"] for q in m["questions"] if q["id"] == f"{SCENARIO_ID}-q{n}")


# --- the phase --------------------------------------------------------------------------------------------
def test_phase_counts_and_byte_identical_rebuild(sc: Scenario, files: dict[str, bytes]) -> None:
    assert build_mod.PHASES.index("lifecycle") == build_mod.PHASES.index("docsets") + 1
    assert tuple(sc.counts[k] for k in ("emails", "attachments", "chats")) == build_mod.EXPECTED_TOTALS["lifecycle"]
    again = next(s for s in build_mod.build().scenarios if s.phase == "lifecycle")
    assert again.files() == files


# --- revisions, redlines and comments ---------------------------------------------------------------------
def test_one_file_name_three_revisions_and_identical_copies(manifest: dict[str, Any]) -> None:
    atts = [a for a in manifest["attachments"] if a["filename"] == FILE]
    assert len(atts) == 5 and len({a["sha256"] for a in atts}) == 3
    revs = manifest["lifecycle"]["revisions"]
    assert [r["revision"] for r in revs] == [1, 2, 3]
    for r in revs:
        assert {a["message"] for a in atts if a["sha256"] == r["sha256"]} == set(r["messages"])
    assert [len(r["messages"]) for r in revs] == [1, 2, 2]  # forward of rev 2, "clean" return of rev 3


def test_redlines_carry_tracked_changes_and_comments_anchor_to_their_clause(
    manifest: dict[str, Any], files: dict[str, bytes]
) -> None:
    expected = {"draft": (0, 0), "shell-redline": (1, 2), "newco-revised": (1, 1)}
    for key, (tracked, n_comments) in expected.items():
        doc, comments = _docx(_att(files, manifest, key, FILE))
        changes = len(doc.findall(f".//{_W}ins")) + len(doc.findall(f".//{_W}del"))
        assert (changes > 0) == bool(tracked), key
        assert (0 if comments is None else len(comments.findall(f"{_W}comment"))) == n_comments, key
    doc, comments = _docx(_att(files, manifest, "shell-redline", FILE))
    assert comments is not None
    anchored = {}
    for p in doc.iter(f"{_W}p"):
        start = p.find(f"{_W}commentRangeStart")
        if start is not None:
            anchored[start.get(f"{_W}id")] = "".join(t.text or "" for t in p.iter(f"{_W}t"))
    texts = {c.get(f"{_W}id"): "".join(t.text or "" for t in c.iter(f"{_W}t")) for c in comments}
    by_text = {texts[i]: anchored[i] for i in texts}
    netting = next(t for t in by_text if "product group" in t)
    assert "netted into" in by_text[netting]
    assert "governed by the law of" in by_text[next(t for t in by_text if "English law" in t)]
    gt = manifest["lifecycle"]["comments"]
    assert gt[2]["replies_to"] == 0 and gt[0]["clause"] == "netting"


# --- threads ----------------------------------------------------------------------------------------------
def test_forwards_reference_the_original_and_carry_its_body(manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    for fw in manifest["lifecycle"]["forwards"]:
        msg = _eml(files, manifest, fw["message"])
        orig = _msg(manifest, fw["forward_of"])
        assert str(msg["Subject"]).startswith("Fwd: ")
        assert msg["In-Reply-To"] is None
        assert orig["message_id"] in str(msg["References"])
        body = _body(files, manifest, fw["message"])
        assert "---------- Forwarded message ---------" in body and orig["subject"] in body


def test_quoted_history_carries_superseded_terms_that_are_not_new_positions(
    manifest: dict[str, Any], files: dict[str, bytes]
) -> None:
    body = _body(files, manifest, "credit-instruction")
    assert "> Shell wants a USD 10m threshold" in body  # quoted, superseded
    events = manifest["lifecycle"]["events"]
    from_credit = [e for e in events if e["message"] == "credit-instruction"]
    assert {(e["clause"], e["value"], e["status"]) for e in from_credit} == {
        ("threshold", 7_500_000, "internal"),
        ("payment", 20, "internal"),
    }
    for q in manifest["lifecycle"]["quoted_history"]:
        assert "wrote:\n> " in _body(files, manifest, q["message"])


def test_duplicate_resend_has_the_same_content_and_a_new_message_id(
    manifest: dict[str, Any], files: dict[str, bytes]
) -> None:
    ((a, b),) = manifest["lifecycle"]["duplicate_messages"]
    assert _body(files, manifest, a) == _body(files, manifest, b)
    assert _msg(manifest, a)["message_id"] != _msg(manifest, b)["message_id"]
    assert [e for e in manifest["lifecycle"]["events"] if e["message"] == b] == []
    assert _answer(manifest, 14) == [_msg(manifest, b)["message_id"]]


def test_executed_copy_is_a_scan_without_text(manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    pdf = pdfium.PdfDocument(_att(files, manifest, "executed", "MPTA_NewCo_Shell_executed.pdf"))
    assert all(pdf[i].get_textpage().get_text_range().strip() == "" for i in range(len(pdf)))


# --- as of ------------------------------------------------------------------------------------------------
def test_every_event_points_at_a_real_message_and_attachment(manifest: dict[str, Any]) -> None:
    keys = {m["key"] for m in manifest["messages"]}
    shas = {(a["message"], a["filename"]): a["sha256"] for a in manifest["attachments"]}
    for e in manifest["lifecycle"]["events"]:
        assert e["message"] in keys
        if e["attachment"]:
            assert shas[(e["message"], e["attachment"])] == e["sha256"]
    seqs = [e["seq"] for e in manifest["lifecycle"]["events"]]
    assert seqs == list(range(1, len(seqs) + 1))


def test_on_table_and_agreed_views_differ_as_negotiation_moves(manifest: dict[str, Any]) -> None:
    ev = manifest["lifecycle"]["events"]
    at_fwd = _when(manifest, "fwd-credit")
    assert value_as_of(ev, "payment", "days", at_fwd, "on_table") == 20 == _answer(manifest, 1)
    assert value_as_of(ev, "payment", "days", at_fwd, "agreed") is None  # nothing binding yet
    assert (
        value_as_of(ev, "threshold", "amount", _when(manifest, "credit-instruction"), "on_table") == 10_000_000
    )  # internal 7.5m not offered
    first = next(e for e in ev if e["clause"] == "threshold" and e["status"] == "agreed")
    assert _msg(manifest, first["message"])["message_id"] == _answer(manifest, 3)
    assert value_as_of(ev, "threshold", "amount", _when(manifest, "shell-agrees")) == 7_500_000


def test_amendment_counts_from_its_effective_date_not_its_signing(manifest: dict[str, Any]) -> None:
    ev = manifest["lifecycle"]["events"]
    (amend,) = manifest["lifecycle"]["agreement"]["amendments"]
    signed = _when(manifest, "amendment-signed")
    eff = datetime.combine(date.fromisoformat(amend["effective_from"]), datetime.min.time(), tzinfo=UTC)
    assert value_as_of(ev, "late_interest", "rate", _when(manifest, "executed")) == 3 == _answer(manifest, 4)
    assert value_as_of(ev, "late_interest", "rate", signed) == 3 == _answer(manifest, 5)
    assert value_as_of(ev, "late_interest", "rate", eff - timedelta(seconds=1)) == 3
    assert value_as_of(ev, "late_interest", "rate", eff + timedelta(hours=12)) == 2.5
    assert value_as_of(ev, "late_interest", "rate", signed, "on_table") == 2.5  # known but not yet in force


def test_executed_terms_equal_the_final_redline_values(manifest: dict[str, Any]) -> None:
    lc = manifest["lifecycle"]
    at_exec = terms_as_of(lc["events"], _when(manifest, "executed"))
    for clause, params in lc["agreement"]["final_terms"].items():
        for p, v in params.items():
            assert at_exec[clause][p]["value"] == v
            assert at_exec[clause][p]["status"] == "executed"


# --- calculations ---------------------------------------------------------------------------------------------
def test_calculations_recompute_from_terms_and_inputs(manifest: dict[str, Any]) -> None:
    lc = manifest["lifecycle"]
    ev = lc["events"]
    calc = {c["id"]: c for c in lc["calculations"]}
    net = calc["netting"]
    amount, payer = net_settlement(Decimal(str(net["inputs"]["receivable"])), Decimal(str(net["inputs"]["payable"])))
    assert float(amount) == net["result"]["net"] and payer == "them"
    days = value_as_of(ev, "payment", "days", datetime.fromisoformat(net["as_of"]))
    assert net["terms"]["payment.days"] == days == 20
    assert date.fromisoformat(net["inputs"]["due"]) >= date.fromisoformat(net["inputs"]["bl_date"]) + timedelta(
        days=days
    )
    li = calc["late-interest"]
    due = datetime.combine(date.fromisoformat(li["as_of"]), datetime.min.time(), tzinfo=UTC)
    rate = value_as_of(ev, "late_interest", "rate", due)
    assert float(li["terms"]["late_interest.rate"]) == rate == 2.5 == _answer(manifest, 6)
    assert (
        float(late_interest(amount, Decimal(str(rate)), li["inputs"]["days_late"]))
        == li["result"]["interest"]
        == _answer(manifest, 11)
    )
    mc = calc["margin-call"]
    assert (
        float(margin_call(Decimal(str(mc["inputs"]["exposure"])), Decimal(7_500_000), Decimal(250_000), Decimal(1)))
        == mc["result"]["call"]
        == _answer(manifest, 12)
    )
    rn = calc["renewal-notice"]
    end, deadline = renewal_notice_deadline(date.fromisoformat(rn["inputs"]["effective"]), 12, 45)
    assert (end.isoformat(), deadline.isoformat()) == (rn["result"]["term_end"], rn["result"]["notice_deadline"])
    assert deadline.isoformat() == _answer(manifest, 13)


def test_calculation_documents_print_the_same_figures(manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    calc = {c["id"]: c for c in manifest["lifecycle"]["calculations"]}

    def text(key: str, name: str) -> str:
        pdf = pdfium.PdfDocument(_att(files, manifest, key, name))
        return " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))

    netting = next(a["filename"] for a in manifest["attachments"] if a["message"] == "netting")
    assert f"{calc['netting']['result']['net']:,.2f}" in text("netting", netting)
    mc = next(a["filename"] for a in manifest["attachments"] if a["message"] == "margin-call")
    assert f"{calc['margin-call']['result']['call']:,.2f}" in text("margin-call", mc)
    assert f"{calc['late-interest']['result']['interest']:,.2f}" in text("late-interest", "late-interest-invoice.pdf")


def test_approvals_precede_execution_and_questions_reach_golden(manifest: dict[str, Any]) -> None:
    executed = _when(manifest, "executed")
    stages = manifest["lifecycle"]["approvals"]
    assert [a["stage"] for a in stages] == ["legal", "credit", "business"] == _answer(manifest, 15)
    assert all(datetime.fromisoformat(a["at"]) < executed for a in stages)
    golden = build_golden(build_mod.build())
    assert {f"{SCENARIO_ID}-q{i}" for i in range(1, 16)} <= set(golden)


# --- writer -------------------------------------------------------------------------------------------------
def test_docx_comments_add_parts_only_when_present() -> None:
    when = datetime(2026, 3, 2, 9, 15, tzinfo=UTC)
    bare = build_docx("t", [plain("x")], author="a", created=when)
    with zipfile.ZipFile(io.BytesIO(bare)) as zf:
        assert "word/comments.xml" not in zf.namelist()
    commented = build_docx(
        "t", [Para((Run("x"),), comments=(Comment("Ann Lee", when, "why?"),))], author="a", created=when
    )
    doc, comments = _docx(commented)
    assert comments is not None and comments[0].get(f"{_W}initials") == "AL"
    assert doc.find(f".//{_W}commentReference") is not None
    with pytest.raises(ValueError):
        build_docx(
            "t", [Para((Run("x"),), comments=(Comment("A", datetime(2026, 1, 1), "naive"),))], author="a", created=when
        )
