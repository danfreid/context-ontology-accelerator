# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Hand-built XLSX (SpreadsheetML) with deterministic bytes.

Same approach as ``docx_writer``: the parts are written directly, archive members use a fixed timestamp, a fixed
order and ``ZIP_STORED``. Strings are inline strings; numbers are written as their exact decimal text.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

_FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

Cell = str | int | float | Decimal | None


@dataclass(frozen=True)
class Sheet:
    """One worksheet: a name (31 characters at most) and its rows, top to bottom."""

    name: str
    rows: Sequence[Sequence[Cell]]


def column(index: int) -> str:
    """Return the column letters for a 0-based index (0 -> A, 26 -> AA)."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value: Cell) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("booleans are not supported; write 'yes' or 'no'")
    if isinstance(value, int | float | Decimal):
        text = format(value, "f") if isinstance(value, Decimal) else repr(value)
        return f'<c r="{ref}"><v>{text}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    rows = []
    for r, row in enumerate(sheet.rows, start=1):
        cells = "".join(_cell(f"{column(c)}{r}", v) for c, v in enumerate(row))
        rows.append(f'<row r="{r}">{cells}</row>')
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_MAIN}">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )


def build_xlsx(sheets: Sequence[Sheet], *, title: str, author: str, created: datetime) -> bytes:
    """Return XLSX bytes. ``created`` comes from the scenario clock and must be timezone-aware."""
    if created.tzinfo is None:
        raise ValueError("created must be timezone-aware")
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")
    names = [s.name for s in sheets]
    if len(set(names)) != len(names) or any(len(n) > 31 or not n for n in names):
        raise ValueError(f"sheet names must be unique, non-empty and at most 31 characters: {names}")
    stamp = created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = len(sheets)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, n + 1)
    )
    parts = [
        (
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{overrides}"
            '<Override PartName="/docProps/core.xml" '
            'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>',
        ),
        (
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/></Relationships>',
        ),
        (
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{_MAIN}" xmlns:r="{_REL}">'
            "<sheets>"
            + "".join(f'<sheet name={quoteattr(s.name)} sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1))
            + "</sheets></workbook>",
        ),
        (
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, n + 1)
            )
            + "</Relationships>",
        ),
        *[(f"xl/worksheets/sheet{i}.xml", _sheet_xml(s)) for i, s in enumerate(sheets, 1)],
        (
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>',
        ),
    ]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, xml in parts:
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, xml.encode("utf-8"))
    return buf.getvalue()
