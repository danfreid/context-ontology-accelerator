# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic PDF output with ReportLab (``invariant=1``, built-in fonts, no images)."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle


@dataclass(frozen=True)
class Heading:
    """A section heading."""

    text: str


@dataclass(frozen=True)
class Text:
    """A paragraph of plain text (escaped; a newline becomes a line break)."""

    text: str


@dataclass(frozen=True)
class KeyValues:
    """A two-column block of label and value pairs."""

    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Grid:
    """A table with a header row, for line items and schedules."""

    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Mono:
    """Monospaced text, such as a SWIFT-style message copy."""

    text: str


Block = Heading | Text | KeyValues | Grid | Mono


def build_pdf(title: str, blocks: list[Block], *, author: str = "NewCo synthetic corpus") -> bytes:
    """Render ``blocks`` to PDF bytes. Equal inputs give equal bytes.

    ``invariant=1`` pins the creation date and document id; the scenario date belongs in the blocks.
    """
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=8, leading=10)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        invariant=1,
        pageCompression=0,
        title=title,
        author=author,
        creator="fsi_commodity generator",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    story: list = [Paragraph(escape(title), styles["Title"]), Spacer(1, 4 * mm)]
    for block in blocks:
        if isinstance(block, Heading):
            story.append(Paragraph(escape(block.text), styles["Heading2"]))
        elif isinstance(block, Text):
            story.append(Paragraph(escape(block.text).replace("\n", "<br/>"), body))
        elif isinstance(block, KeyValues):
            data = [[Paragraph(f"<b>{escape(k)}</b>", body), Paragraph(escape(v), body)] for k, v in block.pairs]
            story.append(
                Table(data, colWidths=[55 * mm, 115 * mm], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
            )
        elif isinstance(block, Grid):
            data = [[Paragraph(f"<b>{escape(c)}</b>", body) for c in block.header]]
            data += [[Paragraph(escape(c), body) for c in row] for row in block.rows]
            story.append(Table(data, repeatRows=1, style=TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey)])))
        elif isinstance(block, Mono):
            story.append(Preformatted(block.text, mono))
        story.append(Spacer(1, 3 * mm))
    doc.build(story)
    return buf.getvalue()
