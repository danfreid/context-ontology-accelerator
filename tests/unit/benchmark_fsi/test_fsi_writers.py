# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Writers: byte-identical output, round-trip of headers, extractable PDF text, tracked-change DOCX."""

from __future__ import annotations

import email
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from typing import Any

import pypdfium2
import pytest

from tests.benchmark.fsi_commodity.clauses import Round, redline_rounds, render
from tests.benchmark.fsi_commodity.writers.docx_writer import Para, Run, build_docx, deleted, inserted, plain
from tests.benchmark.fsi_commodity.writers.eml import AttachmentFile, EmailSpec, Party, build_eml, make_message_id
from tests.benchmark.fsi_commodity.writers.manifest import dumps_json
from tests.benchmark.fsi_commodity.writers.md import build_md
from tests.benchmark.fsi_commodity.writers.pdf_writer import Grid, Heading, KeyValues, Mono, Text, build_pdf

pytestmark = pytest.mark.unit

WHEN = datetime(2026, 3, 2, 9, 15, tzinfo=UTC)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _spec(with_attachment: bool = True, reply: bool = True) -> EmailSpec:
    mid = make_message_id("s1", "m2", "newco-houston.example")
    parent = make_message_id("s1", "m1", "vitol.example")
    return EmailSpec(
        message_id=mid,
        date=WHEN,
        sender=Party("Ann Lee", "ann.lee@newco-houston.example"),
        to=(Party("Bo Chen", "bo.chen@vitol.example"),),
        cc=(Party("Cy Doe", "cy.doe@newco-houston.example"),),
        subject="Re: Master agreement, round 2",
        body="Hello Bo,\n\nPlease see the attached mark-up. Caf\u00e9 terms apply.\n",
        in_reply_to=parent if reply else None,
        references=(parent,) if reply else (),
        attachments=(AttachmentFile("mpta-r2.docx", b"PK-demo-bytes" * 40, "application/octet-stream"),)
        if with_attachment
        else (),
    )


def test_message_id_is_stable_and_domain_scoped() -> None:
    a = make_message_id("s1", "m1", "x.example")
    assert a == make_message_id("s1", "m1", "x.example")
    assert a != make_message_id("s1", "m2", "x.example")
    assert a.startswith("<") and a.endswith("@x.example>")


@pytest.mark.parametrize("with_attachment", [False, True])
def test_eml_is_byte_identical_and_headers_survive(with_attachment: bool) -> None:
    spec = _spec(with_attachment)
    raw = build_eml(spec)
    assert raw == build_eml(spec)
    assert b"\r" not in raw
    msg = email.message_from_bytes(raw)
    assert msg["Message-ID"] == spec.message_id
    assert msg["In-Reply-To"] == spec.in_reply_to
    assert msg["References"] == spec.references[0]
    assert msg["Subject"] == spec.subject
    assert msg["Date"] == "Mon, 02 Mar 2026 09:15:00 +0000"
    assert "bo.chen@vitol.example" in msg["To"]
    if with_attachment:
        assert msg.is_multipart()
        boundary = msg.get_boundary()
        assert boundary is not None and boundary.startswith("=_nc_")
        (att,) = [p for p in msg.walk() if p.get_filename()]
        assert att.get_filename() == "mpta-r2.docx"
        assert att.get_payload(decode=True) == spec.attachments[0].content


def test_eml_rejects_naive_dates() -> None:
    spec = _spec()
    naive = EmailSpec(**{**spec.__dict__, "date": datetime(2026, 3, 2, 9, 15)})
    with pytest.raises(ValueError):
        build_eml(naive)


def test_md_rendering_lists_headers_body_and_attachments() -> None:
    spec = _spec()
    md = build_md(spec, {"mpta-r2.docx": "../attachments/001-mpta-r2.docx"})
    assert md == build_md(spec, {"mpta-r2.docx": "../attachments/001-mpta-r2.docx"})
    text = md.decode("utf-8")
    for needle in (spec.message_id, "**In-Reply-To:**", "**References:**", "Please see the attached", "## Attachments"):
        assert needle in text
    assert "../attachments/001-mpta-r2.docx" in text
    assert spec.attachments[0].sha256 in text


def _pdf() -> bytes:
    return build_pdf(
        "Commercial invoice INV-0001",
        [
            Heading("Parties"),
            KeyValues((("Seller", "NewCo Trading Houston LLC"), ("Buyer", "Vitol Inc."))),
            Grid(
                ("Item", "Qty", "Amount"),
                (("WTI Midland", "250,000", "USD 1,000.00"), ("Premium", "250,000", "USD 25.00")),
            ),
            Text("Pay within 30 days.\nNo deductions."),
            Mono(":20:REF123\n:32A:260302USD5138000,"),
        ],
    )


def test_pdf_is_byte_identical_and_text_is_extractable() -> None:
    data = _pdf()
    assert data == _pdf()
    assert data.startswith(b"%PDF-")
    doc = pypdfium2.PdfDocument(data)
    text = "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    for needle in ("INV-0001", "NewCo Trading Houston LLC", "WTI Midland", "REF123", "5138000"):
        assert needle in text


def _docx() -> bytes:
    paras = [
        plain("Master agreement, round 1", heading=True),
        Para(
            (
                Run("Payment within "),
                deleted("30", "Vitol legal", WHEN),
                inserted("20", "Vitol legal", WHEN),
                Run(" days."),
            )
        ),
        Para((inserted("A new sentence.", "NewCo legal", WHEN),)),
    ]
    return build_docx("MPTA round 1", paras, author="NewCo legal", created=WHEN)


def test_docx_is_byte_identical_and_has_tracked_changes() -> None:
    data = _docx()
    assert data == _docx()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "word/_rels/document.xml.rels",
            "word/document.xml",
            "docProps/core.xml",
        ]
        assert all(i.date_time == (2026, 1, 1, 0, 0, 0) for i in zf.infolist())
        for name in zf.namelist():
            ET.fromstring(zf.read(name))  # every part is well-formed XML
        root = ET.fromstring(zf.read("word/document.xml"))
        core = zf.read("docProps/core.xml").decode()
    assert len(root.findall(f".//{W}ins")) == 2
    assert len(root.findall(f".//{W}del")) == 1
    (dele,) = root.findall(f".//{W}del")
    assert dele.get(f"{W}author") == "Vitol legal"
    assert dele.get(f"{W}date") == "2026-03-02T09:15:00Z"
    del_text = dele.find(f".//{W}delText")
    assert del_text is not None and del_text.text == "30"
    ids_ = [e.get(f"{W}id") for e in root.iter() if e.tag in (f"{W}ins", f"{W}del")]
    assert len(set(ids_)) == len(ids_)
    assert "2026-03-02T09:15:00Z" in core


def test_docx_rejects_naive_created_and_untimed_tracked_runs() -> None:
    with pytest.raises(ValueError):
        build_docx("t", [], author="a", created=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        build_docx("t", [Para((Run("x", "ins", "a", None),))], author="a", created=WHEN)


def test_redline_rounds_track_changes_and_carry_final_values() -> None:
    base: dict[str, dict[str, Any]] = {
        "payment": {"days": 30, "currency": "USD"},
        "quantity_tolerance": {"tolerance": 5},
    }
    rounds = [
        Round("Vitol legal", WHEN, {"payment": {"days": 20}}),
        Round("NewCo legal", WHEN, {"payment": {"days": 25}, "quantity_tolerance": {"tolerance": 10}}),
    ]
    result = redline_rounds("MPTA", base, rounds)
    assert result.final_values == {"payment": {"days": 25, "currency": "USD"}, "quantity_tolerance": {"tolerance": 10}}
    assert result.history["payment"] == [
        (0, base["payment"]),
        (1, {"days": 20, "currency": "USD"}),
        (2, {"days": 25, "currency": "USD"}),
    ]
    assert "within 25 days" in render("payment", result.final_values["payment"])
    docs = [build_docx("MPTA", paras, author="x", created=WHEN) for paras in result.rounds]
    counts = []
    for data in docs:
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
        counts.append((len(root.findall(f".//{W}ins")), len(root.findall(f".//{W}del"))))
    assert counts == [(1, 1), (2, 2)]


def test_manifest_json_is_sorted_indented_and_newline_terminated() -> None:
    out = dumps_json({"b": 1, "a": {"d": [1, 2], "c": "\u00e9"}})
    assert out == b'{\n  "a": {\n    "c": "\xc3\xa9",\n    "d": [\n      1,\n      2\n    ]\n  },\n  "b": 1\n}\n'
