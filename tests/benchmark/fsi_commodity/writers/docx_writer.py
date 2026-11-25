# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Hand-built DOCX with real tracked changes (``w:ins`` / ``w:del``) and deterministic bytes.

python-docx has no tracked-change API and stamps archive members with the wall clock, so the OOXML is
written directly. Members use a fixed timestamp, fixed order and ``ZIP_STORED``, which keeps the bytes
independent of the zlib build.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

_FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True)
class Run:
    """A run of text. ``kind`` is ``text``, ``ins`` (tracked insertion) or ``del`` (tracked deletion)."""

    text: str
    kind: str = "text"
    author: str = ""
    when: datetime | None = None


@dataclass(frozen=True)
class Para:
    """A paragraph made of runs; ``heading`` renders it bold and larger."""

    runs: tuple[Run, ...]
    heading: bool = False


def plain(text: str, heading: bool = False) -> Para:
    """A paragraph of untracked text."""
    return Para((Run(text),), heading)


def inserted(text: str, author: str, when: datetime) -> Run:
    """A tracked insertion."""
    return Run(text, "ins", author, when)


def deleted(text: str, author: str, when: datetime) -> Run:
    """A tracked deletion."""
    return Run(text, "del", author, when)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_xml(run: Run, rid: int, heading: bool) -> str:
    props = '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr>' if heading else ""
    tag = "w:delText" if run.kind == "del" else "w:t"
    inner = f'<w:r>{props}<{tag} xml:space="preserve">{escape(run.text)}</{tag}></w:r>'
    if run.kind == "text":
        return inner
    if run.kind not in ("ins", "del") or run.when is None:
        raise ValueError("tracked runs need kind 'ins' or 'del' and a datetime")
    attrs = f'w:id="{rid}" w:author={quoteattr(run.author)} w:date="{_stamp(run.when)}"'
    return f"<w:{run.kind} {attrs}>{inner}</w:{run.kind}>"


def build_docx(title: str, paragraphs: list[Para], *, author: str, created: datetime) -> bytes:
    """Return DOCX bytes. ``created`` comes from the scenario clock and must be timezone-aware."""
    if created.tzinfo is None:
        raise ValueError("created must be timezone-aware")
    rid = 0
    body: list[str] = []
    for para in paragraphs:
        runs = []
        for run in para.runs:
            rid += 1
            runs.append(_run_xml(run, rid, para.heading))
        body.append(f"<w:p>{''.join(runs)}</w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(body)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
    )
    stamp = _stamp(created)
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>"
        f"<cp:lastModifiedBy>{escape(author)}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>'
    )
    parts = [
        (
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/docProps/core.xml" '
            'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>',
        ),
        (
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/></Relationships>',
        ),
        (
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        ),
        ("word/document.xml", document),
        ("docProps/core.xml", core),
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
